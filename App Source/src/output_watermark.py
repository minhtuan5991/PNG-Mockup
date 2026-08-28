from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from output_metadata import clean_image_metadata_copy


WATERMARK_SIZE = (2000, 2000)
WATERMARK_DPI = 72
WATERMARK_DPI_TOLERANCE = 1.0
SUPPORTED_WATERMARK_TARGET_EXTS = (".png", ".jpg", ".jpeg", ".webp")
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class OutputWatermarkError(RuntimeError):
    """Raised when a watermark cannot be validated or published safely."""


@dataclass(frozen=True)
class WatermarkInfo:
    path: Path
    width: int
    height: int
    dpi_x: float
    dpi_y: float
    has_transparency: bool


@dataclass(frozen=True)
class WatermarkResult:
    source_path: Path
    output_path: Path
    watermark_path: Path
    width: int
    height: int
    output_format: str


@dataclass(frozen=True)
class _CachedWatermark:
    info: WatermarkInfo
    pixels: bytes


def inspect_watermark(
    watermark_path: str | Path,
    *,
    expected_size: tuple[int, int] = WATERMARK_SIZE,
    expected_dpi: int = WATERMARK_DPI,
) -> WatermarkInfo:
    """
    Validate the transparent PNG selected as a full-canvas watermark.

    The cache key contains the file's size and mtime so repeated PSD exports do
    not decode the same 2000x2000 overlay for every output.
    """

    path = Path(watermark_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy ảnh Watermark: {path}")
    stat = path.stat()
    cached = _load_watermark_cached(str(path), stat.st_mtime_ns, stat.st_size)
    info = cached.info

    if (info.width, info.height) != expected_size:
        raise OutputWatermarkError(
            "Ảnh Watermark phải có kích thước "
            f"{expected_size[0]}x{expected_size[1]} px; ảnh đã chọn là "
            f"{info.width}x{info.height} px."
        )
    if (
        abs(info.dpi_x - expected_dpi) > WATERMARK_DPI_TOLERANCE
        or abs(info.dpi_y - expected_dpi) > WATERMARK_DPI_TOLERANCE
    ):
        raise OutputWatermarkError(
            f"Ảnh Watermark phải là {expected_dpi} DPI; ảnh đã chọn là "
            f"{info.dpi_x:.2f}x{info.dpi_y:.2f} DPI."
        )
    if not info.has_transparency:
        raise OutputWatermarkError(
            "Ảnh Watermark phải có nền trong suốt (kênh alpha có điểm ảnh trong suốt)."
        )
    return info


def apply_watermark_atomic(
    image_path: str | Path,
    watermark_path: str | Path,
    log: Any | None = None,
    *,
    expected_size: tuple[int, int] = WATERMARK_SIZE,
    expected_dpi: int = WATERMARK_DPI,
) -> WatermarkResult:
    """
    Overlay a validated watermark and atomically replace one generated PNG.

    The destination is decoded once, composed once, written beside the original
    and fully decoded again before os.replace() publishes it.
    """

    path = Path(image_path).resolve()
    if path.suffix.casefold() != ".png":
        raise OutputWatermarkError(
            f"Watermark tự động chỉ hỗ trợ ảnh mockup PNG: {path.name}"
        )
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy ảnh mockup: {path}")

    watermark = _cached_watermark_for_use(
        watermark_path,
        expected_size=expected_size,
        expected_dpi=expected_dpi,
    )
    with Image.open(path) as source:
        if source.format != "PNG":
            raise OutputWatermarkError(
                f"File mockup không phải PNG hợp lệ: {path.name}"
            )
        if int(getattr(source, "n_frames", 1)) != 1:
            raise OutputWatermarkError(
                f"Ảnh mockup động không được hỗ trợ gắn Watermark: {path.name}"
            )
        source.load()
        if source.size != (watermark.info.width, watermark.info.height):
            raise OutputWatermarkError(
                "Kích thước Watermark không khớp ảnh mockup trong Output: "
                f"{watermark.info.width}x{watermark.info.height} so với "
                f"{source.width}x{source.height} px ({path.name})."
            )
        source_has_alpha = _image_has_alpha(source)
        source_dpi = _normalized_dpi(source.info.get("dpi"))
        if source_dpi is None:
            raise OutputWatermarkError(
                f"Ảnh mockup không có thông tin DPI: {path.name}."
            )
        if (
            abs(source_dpi[0] - watermark.info.dpi_x)
            > WATERMARK_DPI_TOLERANCE
            or abs(source_dpi[1] - watermark.info.dpi_y)
            > WATERMARK_DPI_TOLERANCE
        ):
            raise OutputWatermarkError(
                "DPI Watermark không khớp ảnh mockup trong Output: "
                f"{watermark.info.dpi_x:.2f}x{watermark.info.dpi_y:.2f} so với "
                f"{source_dpi[0]:.2f}x{source_dpi[1]:.2f} DPI ({path.name})."
            )
        composed = _composite(source, watermark)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.watermark-",
        suffix=".png",
        dir=path.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        save_image = composed if source_has_alpha else composed.convert("RGB")
        save_image.save(
            temporary,
            format="PNG",
            dpi=source_dpi or (WATERMARK_DPI, WATERMARK_DPI),
            compress_level=3,
        )
        _verify_watermarked_image(
            temporary,
            expected_format="PNG",
            expected_size=(watermark.info.width, watermark.info.height),
            expected_alpha=source_has_alpha,
            expected_dpi=source_dpi,
        )
        os.replace(temporary, path)
    except Exception as exc:
        if isinstance(exc, (OutputWatermarkError, FileNotFoundError)):
            raise
        raise OutputWatermarkError(
            f"Không thể gắn Watermark an toàn cho {path.name}: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)

    result = WatermarkResult(
        source_path=path,
        output_path=path,
        watermark_path=watermark.info.path,
        width=watermark.info.width,
        height=watermark.info.height,
        output_format="PNG",
    )
    if log is not None:
        log.info(
            "Applied watermark %s to %s, verified %sx%s; metadata cleanup follows if enabled.",
            result.watermark_path,
            result.output_path,
            result.width,
            result.height,
        )
    return result


def create_watermarked_clean_copy(
    source_path: str | Path,
    output_path: str | Path,
    watermark_path: str | Path,
    log: Any | None = None,
) -> WatermarkResult:
    """
    Create a watermarked copy, then remove all requested metadata sections.

    The selected source is never modified. The intermediate watermarked file
    stays hidden beside the destination and is deleted after the existing
    metadata cleaner has validated and published the final `.cleaned` image.
    """

    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    extension = source.suffix.casefold()
    if extension not in SUPPORTED_WATERMARK_TARGET_EXTS:
        raise OutputWatermarkError(
            f"Định dạng ảnh không hỗ trợ gắn Watermark: {source.name}"
        )
    if not source.is_file():
        raise FileNotFoundError(f"Không tìm thấy ảnh nguồn: {source}")
    if output.exists():
        raise FileExistsError(f"File kết quả đã tồn tại: {output}")
    if output.suffix.casefold() != extension:
        raise OutputWatermarkError(
            "Ảnh kết quả phải giữ nguyên phần mở rộng của ảnh nguồn."
        )

    watermark = _cached_watermark_for_use(
        watermark_path,
        expected_size=WATERMARK_SIZE,
        expected_dpi=WATERMARK_DPI,
    )
    with Image.open(source) as image:
        source_format = str(image.format or "").upper()
        expected_format = {
            ".png": "PNG",
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".webp": "WEBP",
        }[extension]
        if source_format != expected_format:
            raise OutputWatermarkError(
                f"Phần mở rộng không khớp định dạng ảnh: {source.name}"
            )
        if int(getattr(image, "n_frames", 1)) != 1:
            raise OutputWatermarkError(
                f"Ảnh động không được hỗ trợ gắn Watermark: {source.name}"
            )
        image.load()
        if image.size != (watermark.info.width, watermark.info.height):
            raise OutputWatermarkError(
                "Kích thước Watermark không khớp ảnh đã chọn: "
                f"{watermark.info.width}x{watermark.info.height} so với "
                f"{image.width}x{image.height} px ({source.name})."
            )
        source_has_alpha = _image_has_alpha(image)
        source_dpi = _normalized_dpi(image.info.get("dpi"))
        composed = _composite(image, watermark)

    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.watermark-",
        suffix=extension,
        dir=output.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        _save_composed_copy(
            composed,
            temporary,
            extension,
            source_has_alpha=source_has_alpha,
            dpi=source_dpi or (WATERMARK_DPI, WATERMARK_DPI),
        )
        _verify_watermarked_image(
            temporary,
            expected_format=expected_format,
            expected_size=(watermark.info.width, watermark.info.height),
            expected_alpha=source_has_alpha if extension == ".png" else None,
            expected_dpi=(
                source_dpi or (WATERMARK_DPI, WATERMARK_DPI)
                if extension == ".png"
                else None
            ),
        )
        # This stream-level pass removes Comment, EXIF, XMP, EXIF thumbnail,
        # IPTC and ICC profile and verifies they are absent before publishing.
        clean_image_metadata_copy(temporary, output, log=log)
    except Exception as exc:
        if isinstance(
            exc,
            (OutputWatermarkError, FileNotFoundError, FileExistsError),
        ):
            raise
        raise OutputWatermarkError(
            f"Không thể tạo ảnh Watermark sạch từ {source.name}: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)

    result = WatermarkResult(
        source_path=source,
        output_path=output,
        watermark_path=watermark.info.path,
        width=watermark.info.width,
        height=watermark.info.height,
        output_format=expected_format,
    )
    if log is not None:
        log.info(
            "Created watermarked and metadata-cleaned copy %s from %s using %s.",
            output,
            source,
            watermark.info.path,
        )
    return result


def watermark_output_folder_name(watermark_path: str | Path) -> str:
    """Return a safe Windows folder name based on the watermark file stem."""

    stem = Path(watermark_path).stem.strip().rstrip(" .")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip().rstrip(" .")
    if not stem:
        stem = "Watermark"
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return stem


def clear_watermark_cache() -> None:
    _load_watermark_cached.cache_clear()


def _cached_watermark_for_use(
    watermark_path: str | Path,
    *,
    expected_size: tuple[int, int],
    expected_dpi: int,
) -> _CachedWatermark:
    info = inspect_watermark(
        watermark_path,
        expected_size=expected_size,
        expected_dpi=expected_dpi,
    )
    stat = info.path.stat()
    return _load_watermark_cached(str(info.path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_watermark_cached(
    resolved_path: str,
    modified_ns: int,
    file_size: int,
) -> _CachedWatermark:
    del modified_ns, file_size
    path = Path(resolved_path)
    if path.suffix.casefold() != ".png":
        raise OutputWatermarkError(
            f"Ảnh Watermark phải có định dạng PNG: {path.name}"
        )
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise OutputWatermarkError(
                    f"File Watermark không phải PNG hợp lệ: {path.name}"
                )
            if int(getattr(image, "n_frames", 1)) != 1:
                raise OutputWatermarkError(
                    f"Ảnh Watermark PNG động/APNG không được hỗ trợ: {path.name}"
                )
            image.load()
            dpi = _normalized_dpi(image.info.get("dpi"))
            if dpi is None:
                raise OutputWatermarkError(
                    f"Ảnh Watermark không có thông tin DPI: {path.name}. "
                    f"Hãy lưu ảnh ở {WATERMARK_DPI} DPI."
                )
            rgba = image.convert("RGBA")
            alpha_min, alpha_max = rgba.getchannel("A").getextrema()
            if alpha_max == 0:
                raise OutputWatermarkError(
                    "Ảnh Watermark đang trong suốt hoàn toàn nên không có nội dung để gắn."
                )
            info = WatermarkInfo(
                path=path,
                width=rgba.width,
                height=rgba.height,
                dpi_x=dpi[0],
                dpi_y=dpi[1],
                has_transparency=alpha_min < 255,
            )
            return _CachedWatermark(info=info, pixels=rgba.tobytes())
    except OutputWatermarkError:
        raise
    except Exception as exc:
        raise OutputWatermarkError(
            f"Không đọc được ảnh Watermark {path.name}: {exc}"
        ) from exc


def _composite(source: Image.Image, watermark: _CachedWatermark) -> Image.Image:
    base = source.convert("RGBA")
    overlay = Image.frombytes(
        "RGBA",
        (watermark.info.width, watermark.info.height),
        watermark.pixels,
    )
    base.alpha_composite(overlay)
    return base


def _save_composed_copy(
    composed: Image.Image,
    path: Path,
    extension: str,
    *,
    source_has_alpha: bool,
    dpi: tuple[float, float],
) -> None:
    if extension == ".png":
        image = composed if source_has_alpha else composed.convert("RGB")
        image.save(path, format="PNG", dpi=dpi, compress_level=3)
    elif extension in {".jpg", ".jpeg"}:
        composed.convert("RGB").save(
            path,
            format="JPEG",
            quality=95,
            subsampling=0,
            optimize=False,
            dpi=dpi,
        )
    elif extension == ".webp":
        image = composed if source_has_alpha else composed.convert("RGB")
        image.save(path, format="WEBP", lossless=True, method=3)
    else:  # pragma: no cover - guarded by the caller.
        raise OutputWatermarkError(f"Định dạng ảnh không hỗ trợ: {extension}")


def _verify_watermarked_image(
    path: Path,
    *,
    expected_format: str,
    expected_size: tuple[int, int],
    expected_alpha: bool | None,
    expected_dpi: tuple[float, float] | None,
) -> None:
    try:
        with Image.open(path) as image:
            if image.format != expected_format:
                raise OutputWatermarkError(
                    f"Ảnh tạm đổi định dạng thành {image.format or 'không xác định'}."
                )
            if int(getattr(image, "n_frames", 1)) != 1:
                raise OutputWatermarkError("Ảnh Watermark tạm đã trở thành ảnh động.")
            image.load()
            if image.size != expected_size:
                raise OutputWatermarkError(
                    f"Ảnh tạm đổi kích thước thành {image.width}x{image.height} px."
                )
            if expected_alpha is not None and _image_has_alpha(image) != expected_alpha:
                raise OutputWatermarkError(
                    "Ảnh tạm đã thay đổi trạng thái alpha/transparency."
                )
            if expected_dpi is not None:
                actual_dpi = _normalized_dpi(image.info.get("dpi"))
                if actual_dpi is None:
                    raise OutputWatermarkError("Ảnh Watermark tạm bị mất thông tin DPI.")
                if (
                    abs(actual_dpi[0] - expected_dpi[0])
                    > WATERMARK_DPI_TOLERANCE
                    or abs(actual_dpi[1] - expected_dpi[1])
                    > WATERMARK_DPI_TOLERANCE
                ):
                    raise OutputWatermarkError(
                        "Ảnh Watermark tạm đã đổi DPI thành "
                        f"{actual_dpi[0]:.2f}x{actual_dpi[1]:.2f}."
                    )
    except OutputWatermarkError:
        raise
    except Exception as exc:
        raise OutputWatermarkError(f"Không mở được ảnh Watermark tạm: {exc}") from exc


def _normalized_dpi(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        try:
            dpi_x = float(value[0])
            dpi_y = float(value[1])
        except (TypeError, ValueError):
            return None
        if dpi_x > 0 and dpi_y > 0:
            return dpi_x, dpi_y
    return None


def _image_has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


APP_ROOT = Path(__file__).resolve().parents[1]
OXIPNG_VERSION = "10.2.0"
OXIPNG_OPT_LEVEL = 4
OXIPNG_THREADS = 2
OXIPNG_TIMEOUT_SEC = 900
DEFAULT_OXIPNG_PATH = APP_ROOT / "tools" / "oxipng" / "oxipng.exe"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DISPLAY_CHUNKS = {
    b"IHDR",
    b"PLTE",
    b"tRNS",
    b"cHRM",
    b"gAMA",
    b"sRGB",
    b"cICP",
    b"pHYs",
    b"sBIT",
    b"bKGD",
    b"hIST",
}


@dataclass(frozen=True)
class PngImageInfo:
    path: Path
    width: int
    height: int
    mode: str
    alpha_extrema: tuple[int, int]

    @property
    def long_edge(self) -> int:
        return max(self.width, self.height)

    @property
    def can_resize_transparent(self) -> bool:
        # Match the resize pipeline, which removes alpha values below 2.
        # A fully opaque image has no transparent background; an image whose
        # strongest alpha is 0 or 1 has no visible content after cleanup.
        return self.alpha_extrema != (255, 255) and self.alpha_extrema[1] >= 2


@dataclass(frozen=True)
class PngOptimizationResult:
    output_path: Path
    input_bytes: int
    output_bytes: int

    @property
    def saved_bytes(self) -> int:
        return max(0, self.input_bytes - self.output_bytes)


@dataclass(frozen=True)
class PngProcessingResult:
    output_path: Path
    resized: bool
    compressed: bool
    input_bytes_for_compression: int
    output_bytes: int

    @property
    def saved_bytes(self) -> int:
        if not self.compressed:
            return 0
        return max(0, self.input_bytes_for_compression - self.output_bytes)


@dataclass(frozen=True)
class _PngFingerprint:
    size: tuple[int, int]
    mode: str
    pixel_sha256: str
    metadata: tuple[tuple[str, Any], ...]
    display_chunks: tuple[tuple[str, Any], ...]


def inspect_png(path: Path) -> PngImageInfo:
    source_path = path.resolve()
    if source_path.suffix.casefold() != ".png":
        raise ValueError(f"Chỉ hỗ trợ file PNG: {source_path.name}")
    if not source_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file: {source_path}")

    try:
        with Image.open(source_path) as source:
            if str(source.format or "").casefold() != "png":
                raise ValueError(f"File không phải PNG hợp lệ: {source_path.name}")
            frame_count = int(getattr(source, "n_frames", 1) or 1)
            if frame_count != 1:
                raise ValueError(
                    f"PNG động/APNG không được hỗ trợ: {source_path.name}"
                )
            source.load()
            width, height = source.size
            mode = str(source.mode)
            if "A" in source.getbands():
                alpha = source.getchannel("A")
                try:
                    alpha_extrema = alpha.getextrema()
                finally:
                    alpha.close()
            elif "transparency" in source.info:
                rgba = source.convert("RGBA")
                try:
                    alpha = rgba.getchannel("A")
                    try:
                        alpha_extrema = alpha.getextrema()
                    finally:
                        alpha.close()
                finally:
                    rgba.close()
            else:
                alpha_extrema = (255, 255)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(
            ("File không phải PNG", "PNG động/APNG")
        ):
            raise
        raise ValueError(f"Không đọc được PNG {source_path.name}: {exc}") from exc

    if not isinstance(alpha_extrema, tuple) or len(alpha_extrema) != 2:
        raise ValueError(f"Không đọc được kênh alpha của PNG: {source_path.name}")
    return PngImageInfo(
        path=source_path,
        width=width,
        height=height,
        mode=mode,
        alpha_extrema=(int(alpha_extrema[0]), int(alpha_extrema[1])),
    )


def optimize_png_lossless(
    source_path: Path,
    output_path: Path,
    *,
    executable: Path | None = None,
    timeout_sec: int = OXIPNG_TIMEOUT_SEC,
) -> PngOptimizationResult:
    source = source_path.resolve()
    output = output_path.resolve()
    oxipng = (executable or DEFAULT_OXIPNG_PATH).resolve()

    inspect_png(source)
    if output.suffix.casefold() != ".png":
        raise ValueError(f"Output phải là file PNG: {output.name}")
    if source == output:
        raise ValueError("Nén PNG phải ghi ra file mới để giữ nguyên ảnh nguồn.")
    if output.exists():
        raise FileExistsError(f"Output đã tồn tại: {output}")
    if not oxipng.is_file():
        raise FileNotFoundError(
            "Không tìm thấy oxipng đi kèm ứng dụng. Hãy cài lại hoặc cập nhật "
            f"PNG Mockup: {oxipng}"
        )
    if timeout_sec <= 0:
        raise ValueError("Timeout oxipng phải lớn hơn 0.")

    source_fingerprint = _png_fingerprint(source)
    input_bytes = source.stat().st_size
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.{uuid.uuid4().hex}.oxipng.tmp.png"
    )
    command = [
        str(oxipng),
        "-o",
        str(OXIPNG_OPT_LEVEL),
        "--nx",
        "--quiet",
        "--threads",
        str(OXIPNG_THREADS),
        "--out",
        str(temporary),
        str(source),
    ]
    creationflags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    )
    try:
        try:
            result = subprocess.run(
                command,
                cwd=oxipng.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"oxipng quá thời gian {timeout_sec} giây khi nén {source.name}."
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Không chạy được oxipng: {exc}") from exc

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "không có chi tiết").strip()
            raise RuntimeError(
                f"oxipng lỗi (exit code {result.returncode}) với {source.name}: "
                f"{details}"
            )
        if not temporary.is_file():
            raise RuntimeError(f"oxipng không tạo output cho {source.name}.")

        optimized_fingerprint = _png_fingerprint(temporary)
        if optimized_fingerprint != source_fingerprint:
            raise RuntimeError(
                f"Kết quả nén không giữ nguyên pixel/màu/ICC/DPI của {source.name}."
            )
        # On Windows, rename fails if another process creates the destination
        # after our initial exists check. This preserves that user's file
        # instead of overwriting it during the final publish step.
        temporary.rename(output)
        return PngOptimizationResult(
            output_path=output,
            input_bytes=input_bytes,
            output_bytes=output.stat().st_size,
        )
    finally:
        temporary.unlink(missing_ok=True)


def process_png_file(
    source_path: Path,
    output_path: Path,
    *,
    output_layout: str | None,
    compress: bool,
    dpi: int,
    executable: Path | None = None,
) -> PngProcessingResult:
    if output_layout is None and not compress:
        raise ValueError("Hãy chọn kích thước hoặc bật Nén ảnh PNG.")

    source = source_path.resolve()
    output = output_path.resolve()
    if output.exists():
        raise FileExistsError(f"Output đã tồn tại: {output}")

    if output_layout is None:
        optimized = optimize_png_lossless(
            source,
            output,
            executable=executable,
        )
        return PngProcessingResult(
            output_path=output,
            resized=False,
            compressed=True,
            input_bytes_for_compression=optimized.input_bytes,
            output_bytes=optimized.output_bytes,
        )

    from transparent_image_layout import resize_transparent_png

    if not compress:
        resize_transparent_png(source, output, output_layout, dpi=dpi)
        return PngProcessingResult(
            output_path=output,
            resized=True,
            compressed=False,
            input_bytes_for_compression=output.stat().st_size,
            output_bytes=output.stat().st_size,
        )

    staging = output.with_name(
        f".{output.stem}.{uuid.uuid4().hex}.resize.tmp.png"
    )
    try:
        resize_transparent_png(source, staging, output_layout, dpi=dpi)
        optimized = optimize_png_lossless(
            staging,
            output,
            executable=executable,
        )
        return PngProcessingResult(
            output_path=output,
            resized=True,
            compressed=True,
            input_bytes_for_compression=optimized.input_bytes,
            output_bytes=optimized.output_bytes,
        )
    finally:
        staging.unlink(missing_ok=True)


def _png_fingerprint(path: Path) -> _PngFingerprint:
    try:
        with Image.open(path) as image:
            if str(image.format or "").casefold() != "png":
                raise ValueError("không phải PNG")
            frame_count = int(getattr(image, "n_frames", 1) or 1)
            if frame_count != 1:
                raise ValueError("PNG động/APNG không được hỗ trợ")
            image.load()
            pixel_sha256 = hashlib.sha256(image.tobytes()).hexdigest()
            metadata = tuple(
                sorted(
                    (str(key), _normalize_metadata(value))
                    for key, value in image.info.items()
                )
            )
            size = (int(image.width), int(image.height))
            mode = str(image.mode)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Không xác minh được PNG {path.name}: {exc}") from exc
    return _PngFingerprint(
        size=size,
        mode=mode,
        pixel_sha256=pixel_sha256,
        metadata=metadata,
        display_chunks=_read_display_chunks(path),
    )


def _normalize_metadata(value: Any) -> Any:
    if isinstance(value, bytes):
        return ("bytes", len(value), hashlib.sha256(value).hexdigest())
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_normalize_metadata(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _normalize_metadata(item))
                for key, item in value.items()
            )
        )
    return repr(value)


def _read_display_chunks(path: Path) -> tuple[tuple[str, Any], ...]:
    chunks: list[tuple[str, Any]] = []
    with path.open("rb") as stream:
        if stream.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            raise RuntimeError(f"Sai PNG signature: {path.name}")
        while True:
            length_bytes = stream.read(4)
            if not length_bytes:
                break
            if len(length_bytes) != 4:
                raise RuntimeError(f"PNG bị cắt ngắn: {path.name}")
            length = int.from_bytes(length_bytes, "big")
            chunk_type = stream.read(4)
            if len(chunk_type) != 4:
                raise RuntimeError(f"PNG bị cắt ngắn: {path.name}")
            data = stream.read(length)
            crc = stream.read(4)
            if len(data) != length or len(crc) != 4:
                raise RuntimeError(f"PNG bị cắt ngắn: {path.name}")
            if chunk_type in DISPLAY_CHUNKS:
                chunks.append((chunk_type.decode("ascii"), data))
            elif chunk_type == b"iCCP":
                chunks.append(("iCCP", _decode_iccp(data, path.name)))
            if chunk_type == b"IEND":
                break
    return tuple(sorted(chunks, key=lambda item: (item[0], repr(item[1]))))


def _decode_iccp(data: bytes, filename: str) -> bytes:
    try:
        _profile_name, compressed = data.split(b"\x00", 1)
        if not compressed or compressed[0] != 0:
            raise ValueError("unsupported compression method")
        # The iCCP keyword is only a descriptive label. Oxipng may normalize it
        # (for example, "ICC Profile" -> "icc") without changing the embedded
        # color profile. Compare the decompressed ICC payload, which is the data
        # that determines color rendering.
        return zlib.decompress(compressed[1:])
    except (ValueError, zlib.error) as exc:
        raise RuntimeError(f"Không đọc được ICC profile trong {filename}: {exc}") from exc

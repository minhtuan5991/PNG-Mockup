from __future__ import annotations

import hashlib
import math
import os
import subprocess
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops


APP_ROOT = Path(__file__).resolve().parents[1]
OXIPNG_VERSION = "10.2.0"
OXIPNG_OPT_LEVEL = 4
OXIPNG_THREADS = 2
OXIPNG_TIMEOUT_SEC = 900
DEFAULT_OXIPNG_PATH = APP_ROOT / "tools" / "oxipng" / "oxipng.exe"
LIBWEBP_VERSION = "1.6.0"
DEFAULT_CWEBP_PATH = APP_ROOT / "tools" / "libwebp" / "cwebp.exe"
DEFAULT_DWEBP_PATH = APP_ROOT / "tools" / "libwebp" / "dwebp.exe"
PNGQUANT_VERSION = "2.17.0"
DEFAULT_PNGQUANT_PATH = APP_ROOT / "tools" / "pngquant" / "pngquant.exe"
POD_TARGET_SAVINGS = 0.35
POD_PALETTE_COLORS = 256
POD_PALETTE_MIN_QUALITY = 80
POD_PALETTE_MAX_QUALITY = 95
POD_PALETTE_SPEED = 4
POD_PALETTE_MIN_PSNR_DB = 37.0
POD_PALETTE_MAX_CHANNEL_DELTA = 100
POD_PRIMARY_NEAR_LOSSLESS = 50
POD_SECONDARY_NEAR_LOSSLESS = 30
POD_RECOVERY_NEAR_LOSSLESS = 70
POD_MIN_PSNR_DB = 48.0
POD_PRIMARY_MAX_CHANNEL_DELTA = 4
POD_SECONDARY_MAX_CHANNEL_DELTA = 8
POD_RECOVERY_MAX_CHANNEL_DELTA = 2
POD_WEBP_METHOD = 4
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
PRINT_CHUNKS = {b"cHRM", b"gAMA", b"sRGB", b"cICP", b"pHYs", b"iCCP"}


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
    compression_mode: str = "lossless"
    target_achieved: bool = False
    psnr_db: float | None = None
    max_channel_delta: int = 0

    @property
    def saved_bytes(self) -> int:
        return max(0, self.input_bytes - self.output_bytes)

    @property
    def saved_fraction(self) -> float:
        return self.saved_bytes / self.input_bytes if self.input_bytes else 0.0


@dataclass(frozen=True)
class PngProcessingResult:
    output_path: Path
    resized: bool
    compressed: bool
    input_bytes_for_compression: int
    output_bytes: int
    compression_mode: str = ""
    target_achieved: bool = False

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


@dataclass(frozen=True)
class _PngQualityMetrics:
    psnr_db: float
    max_channel_delta: int
    alpha_exact: bool


@dataclass(frozen=True)
class _PngCandidate:
    path: Path
    quality: int
    metrics: _PngQualityMetrics
    output_bytes: int


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


def optimize_png_for_pod(
    source_path: Path,
    output_path: Path,
    *,
    cwebp_executable: Path | None = None,
    dwebp_executable: Path | None = None,
    pngquant_executable: Path | None = None,
    oxipng_executable: Path | None = None,
    timeout_sec: int = OXIPNG_TIMEOUT_SEC,
) -> PngOptimizationResult:
    """Compress one PNG with a quality-gated, automatic POD workflow.

    The user does not choose a preset. The primary pass creates an efficient
    8-bit PNG palette with adaptive dithering, matching the kind of output used
    by high-ratio online PNG compressors. A quality gate protects visible POD
    rendering, and ICC/DPI/color chunks are copied from the source. The older
    near-lossless path remains an automatic fallback when palette conversion is
    unsuitable; there is still only one user-visible compression workflow.
    """

    source = source_path.resolve()
    output = output_path.resolve()
    cwebp = (cwebp_executable or DEFAULT_CWEBP_PATH).resolve()
    dwebp = (dwebp_executable or DEFAULT_DWEBP_PATH).resolve()
    pngquant = (pngquant_executable or DEFAULT_PNGQUANT_PATH).resolve()
    oxipng = (oxipng_executable or DEFAULT_OXIPNG_PATH).resolve()

    info = inspect_png(source)
    if output.suffix.casefold() != ".png":
        raise ValueError(f"Output phải là file PNG: {output.name}")
    if source == output:
        raise ValueError("Nén PNG phải ghi ra file mới để giữ nguyên ảnh nguồn.")
    if output.exists():
        raise FileExistsError(f"Output đã tồn tại: {output}")
    if timeout_sec <= 0:
        raise ValueError("Timeout nén PNG phải lớn hơn 0.")

    # cwebp is an 8-bit codec. Keep 16-bit and unusual PNG modes fully lossless
    # instead of silently reducing their precision.
    if info.mode not in {"RGB", "RGBA", "P", "L", "LA", "PA"}:
        fallback = optimize_png_lossless(
            source, output, executable=oxipng, timeout_sec=timeout_sec
        )
        return PngOptimizationResult(
            output_path=fallback.output_path,
            input_bytes=fallback.input_bytes,
            output_bytes=fallback.output_bytes,
            compression_mode="lossless-precision-fallback",
            target_achieved=fallback.saved_fraction >= POD_TARGET_SAVINGS,
        )

    if not oxipng.is_file():
        raise FileNotFoundError(
            "Không tìm thấy oxipng đi kèm ứng dụng. Hãy cài lại hoặc cập nhật "
            f"PNG Mockup: {oxipng}"
        )

    input_bytes = source.stat().st_size
    output.parent.mkdir(parents=True, exist_ok=True)
    candidates: list[_PngCandidate] = []
    selected: _PngCandidate | None = None
    try:
        if pngquant.is_file():
            try:
                palette = _build_palette_candidate(
                    source,
                    output,
                    pngquant=pngquant,
                    oxipng=oxipng,
                    timeout_sec=timeout_sec,
                )
                candidates.append(palette)
                if palette.output_bytes < input_bytes:
                    if output.exists():
                        raise FileExistsError(f"Output đã tồn tại: {output}")
                    palette.path.rename(output)
                    saved_fraction = max(
                        0.0, 1.0 - output.stat().st_size / input_bytes
                    )
                    return PngOptimizationResult(
                        output_path=output,
                        input_bytes=input_bytes,
                        output_bytes=output.stat().st_size,
                        compression_mode=f"pod-palette-{POD_PALETTE_COLORS}",
                        target_achieved=saved_fraction >= POD_TARGET_SAVINGS,
                        psnr_db=palette.metrics.psnr_db,
                        max_channel_delta=palette.metrics.max_channel_delta,
                    )
            except (OSError, RuntimeError, ValueError):
                pass

        missing = [path for path in (cwebp, dwebp) if not path.is_file()]
        if missing:
            fallback = optimize_png_lossless(
                source, output, executable=oxipng, timeout_sec=timeout_sec
            )
            return PngOptimizationResult(
                output_path=fallback.output_path,
                input_bytes=fallback.input_bytes,
                output_bytes=fallback.output_bytes,
                compression_mode="lossless-tool-fallback",
                target_achieved=fallback.saved_fraction >= POD_TARGET_SAVINGS,
            )

        try:
            primary = _build_near_lossless_candidate(
                source,
                output,
                quality=POD_PRIMARY_NEAR_LOSSLESS,
                max_channel_delta=POD_PRIMARY_MAX_CHANNEL_DELTA,
                cwebp=cwebp,
                dwebp=dwebp,
                oxipng=oxipng,
                timeout_sec=timeout_sec,
            )
            candidates.append(primary)
        except (OSError, RuntimeError, ValueError):
            primary = None

        if primary is not None:
            selected = primary
            primary_saving = max(0.0, 1.0 - primary.output_bytes / input_bytes)
            if primary_saving < POD_TARGET_SAVINGS:
                try:
                    secondary = _build_near_lossless_candidate(
                        source,
                        output,
                        quality=POD_SECONDARY_NEAR_LOSSLESS,
                        max_channel_delta=POD_SECONDARY_MAX_CHANNEL_DELTA,
                        cwebp=cwebp,
                        dwebp=dwebp,
                        oxipng=oxipng,
                        timeout_sec=timeout_sec,
                    )
                    candidates.append(secondary)
                    if secondary.output_bytes < selected.output_bytes:
                        selected = secondary
                except (OSError, RuntimeError, ValueError):
                    pass
        else:
            try:
                recovery = _build_near_lossless_candidate(
                    source,
                    output,
                    quality=POD_RECOVERY_NEAR_LOSSLESS,
                    max_channel_delta=POD_RECOVERY_MAX_CHANNEL_DELTA,
                    cwebp=cwebp,
                    dwebp=dwebp,
                    oxipng=oxipng,
                    timeout_sec=timeout_sec,
                )
                candidates.append(recovery)
                selected = recovery
            except (OSError, RuntimeError, ValueError):
                selected = None

        if selected is None or selected.output_bytes >= input_bytes:
            fallback = optimize_png_lossless(
                source, output, executable=oxipng, timeout_sec=timeout_sec
            )
            return PngOptimizationResult(
                output_path=fallback.output_path,
                input_bytes=fallback.input_bytes,
                output_bytes=fallback.output_bytes,
                compression_mode="lossless-quality-fallback",
                target_achieved=fallback.saved_fraction >= POD_TARGET_SAVINGS,
            )

        if output.exists():
            raise FileExistsError(f"Output đã tồn tại: {output}")
        selected.path.rename(output)
        saved_fraction = max(0.0, 1.0 - output.stat().st_size / input_bytes)
        return PngOptimizationResult(
            output_path=output,
            input_bytes=input_bytes,
            output_bytes=output.stat().st_size,
            compression_mode=f"pod-near-lossless-{selected.quality}",
            target_achieved=saved_fraction >= POD_TARGET_SAVINGS,
            psnr_db=selected.metrics.psnr_db,
            max_channel_delta=selected.metrics.max_channel_delta,
        )
    finally:
        for candidate in candidates:
            candidate.path.unlink(missing_ok=True)


def _build_palette_candidate(
    source: Path,
    output: Path,
    *,
    pngquant: Path,
    oxipng: Path,
    timeout_sec: int,
) -> _PngCandidate:
    token = uuid.uuid4().hex
    prefix = output.with_name(f".{output.stem}.{token}.palette")
    quantized_path = prefix.with_suffix(".quantized.tmp.png")
    metadata_path = prefix.with_suffix(".metadata.tmp.png")
    optimized_path = prefix.with_suffix(".optimized.tmp.png")
    success = False
    try:
        _run_image_tool(
            [
                str(pngquant),
                "--quality",
                f"{POD_PALETTE_MIN_QUALITY}-{POD_PALETTE_MAX_QUALITY}",
                "--speed",
                str(POD_PALETTE_SPEED),
                str(POD_PALETTE_COLORS),
                "--force",
                "--output",
                str(quantized_path),
                "--",
                str(source),
            ],
            executable=pngquant,
            timeout_sec=timeout_sec,
            source_name=source.name,
        )
        if not quantized_path.is_file():
            raise RuntimeError(f"pngquant không tạo output cho {source.name}.")

        _copy_print_chunks(source, quantized_path, metadata_path)
        _run_image_tool(
            [
                str(oxipng),
                "-o",
                "2",
                "--alpha",
                "--quiet",
                "--threads",
                str(OXIPNG_THREADS),
                "--out",
                str(optimized_path),
                str(metadata_path),
            ],
            executable=oxipng,
            timeout_sec=timeout_sec,
            source_name=source.name,
        )
        if not optimized_path.is_file():
            raise RuntimeError(f"oxipng không tạo output cho {source.name}.")

        metrics = _validate_pod_candidate(source, optimized_path)
        if (
            metrics.psnr_db < POD_PALETTE_MIN_PSNR_DB
            or metrics.max_channel_delta > POD_PALETTE_MAX_CHANNEL_DELTA
        ):
            raise ValueError(
                "Kết quả palette không đạt cổng chất lượng in POD "
                f"(PSNR {metrics.psnr_db:.2f} dB, sai khác tối đa "
                f"{metrics.max_channel_delta})."
            )
        success = True
        return _PngCandidate(
            path=optimized_path,
            quality=POD_PALETTE_COLORS,
            metrics=metrics,
            output_bytes=optimized_path.stat().st_size,
        )
    finally:
        quantized_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        if not success:
            optimized_path.unlink(missing_ok=True)


def _build_near_lossless_candidate(
    source: Path,
    output: Path,
    *,
    quality: int,
    max_channel_delta: int,
    cwebp: Path,
    dwebp: Path,
    oxipng: Path,
    timeout_sec: int,
) -> _PngCandidate:
    token = uuid.uuid4().hex
    prefix = output.with_name(f".{output.stem}.{token}.q{quality}")
    webp_path = prefix.with_suffix(".tmp.webp")
    decoded_path = prefix.with_suffix(".decoded.tmp.png")
    restored_path = prefix.with_suffix(".restored.tmp.png")
    metadata_path = prefix.with_suffix(".metadata.tmp.png")
    optimized_path = prefix.with_suffix(".optimized.tmp.png")
    success = False
    try:
        _run_image_tool(
            [
                str(cwebp),
                "-quiet",
                "-lossless",
                "-near_lossless",
                str(quality),
                "-m",
                str(POD_WEBP_METHOD),
                "-mt",
                "-alpha_q",
                "100",
                "-alpha_filter",
                "best",
                "-metadata",
                "none",
                str(source),
                "-o",
                str(webp_path),
            ],
            executable=cwebp,
            timeout_sec=timeout_sec,
            source_name=source.name,
        )
        _run_image_tool(
            [str(dwebp), "-quiet", str(webp_path), "-o", str(decoded_path)],
            executable=dwebp,
            timeout_sec=timeout_sec,
            source_name=source.name,
        )
        if not decoded_path.is_file():
            raise RuntimeError(f"dwebp không tạo output cho {source.name}.")

        _restore_source_alpha(source, decoded_path, restored_path)
        _copy_print_chunks(source, restored_path, metadata_path)
        _run_image_tool(
            [
                str(oxipng),
                "-o",
                "2",
                "--alpha",
                "--quiet",
                "--threads",
                str(OXIPNG_THREADS),
                "--out",
                str(optimized_path),
                str(metadata_path),
            ],
            executable=oxipng,
            timeout_sec=timeout_sec,
            source_name=source.name,
        )
        if not optimized_path.is_file():
            raise RuntimeError(f"oxipng không tạo output cho {source.name}.")

        metrics = _validate_pod_candidate(source, optimized_path)
        if (
            not metrics.alpha_exact
            or metrics.max_channel_delta > max_channel_delta
            or metrics.psnr_db < POD_MIN_PSNR_DB
        ):
            raise ValueError(
                f"Kết quả q{quality} không đạt cổng chất lượng in POD "
                f"(PSNR {metrics.psnr_db:.2f} dB, sai khác tối đa "
                f"{metrics.max_channel_delta})."
            )
        success = True
        return _PngCandidate(
            path=optimized_path,
            quality=quality,
            metrics=metrics,
            output_bytes=optimized_path.stat().st_size,
        )
    finally:
        for temporary in (webp_path, decoded_path, restored_path, metadata_path):
            temporary.unlink(missing_ok=True)
        if not success:
            optimized_path.unlink(missing_ok=True)


def _run_image_tool(
    command: list[str],
    *,
    executable: Path,
    timeout_sec: int,
    source_name: str,
) -> None:
    creationflags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    )
    try:
        result = subprocess.run(
            command,
            cwd=executable.parent,
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
            f"{executable.name} quá thời gian {timeout_sec} giây khi nén {source_name}."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Không chạy được {executable.name}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "không có chi tiết").strip()
        raise RuntimeError(
            f"{executable.name} lỗi (exit code {result.returncode}) với "
            f"{source_name}: {details}"
        )


def _source_has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _restore_source_alpha(source_path: Path, decoded_path: Path, output_path: Path) -> None:
    with Image.open(source_path) as source, Image.open(decoded_path) as decoded:
        source.load()
        decoded.load()
        if decoded.size != source.size:
            raise RuntimeError("Kết quả nén đã thay đổi kích thước ảnh.")
        if _source_has_alpha(source):
            source_rgba = source.convert("RGBA")
            candidate = decoded.convert("RGBA")
            alpha = source_rgba.getchannel("A")
            try:
                candidate.putalpha(alpha)
                candidate.save(output_path, format="PNG", compress_level=1)
            finally:
                alpha.close()
                source_rgba.close()
                candidate.close()
        else:
            candidate = decoded.convert("RGB")
            try:
                candidate.save(output_path, format="PNG", compress_level=1)
            finally:
                candidate.close()


def _validate_pod_candidate(source_path: Path, candidate_path: Path) -> _PngQualityMetrics:
    with Image.open(source_path) as source, Image.open(candidate_path) as candidate:
        source.load()
        candidate.load()
        if source.size != candidate.size:
            raise RuntimeError("Kết quả nén đã thay đổi kích thước ảnh.")
        if int(getattr(candidate, "n_frames", 1) or 1) != 1:
            raise RuntimeError("Kết quả nén không phải PNG tĩnh.")
        source_rgba = source.convert("RGBA")
        candidate_rgba = candidate.convert("RGBA")
        try:
            metrics = _measure_composited_quality(source_rgba, candidate_rgba)
        finally:
            source_rgba.close()
            candidate_rgba.close()

    if _print_chunk_fingerprint(source_path) != _print_chunk_fingerprint(candidate_path):
        raise RuntimeError("Kết quả nén không giữ nguyên ICC/DPI và thông tin màu in.")
    return metrics


def _measure_composited_quality(
    source: Image.Image, candidate: Image.Image, *, tile_size: int = 512
) -> _PngQualityMetrics:
    source_alpha = source.getchannel("A")
    candidate_alpha = candidate.getchannel("A")
    try:
        alpha_exact = ImageChops.difference(source_alpha, candidate_alpha).getbbox() is None
    finally:
        source_alpha.close()
        candidate_alpha.close()

    squared_error = 0
    channel_count = source.width * source.height * 3
    maximum = 0
    for top in range(0, source.height, tile_size):
        for left in range(0, source.width, tile_size):
            box = (
                left,
                top,
                min(source.width, left + tile_size),
                min(source.height, top + tile_size),
            )
            source_tile = source.crop(box)
            candidate_tile = candidate.crop(box)
            background = Image.new("RGBA", source_tile.size, (255, 255, 255, 255))
            source_print = Image.alpha_composite(background, source_tile).convert("RGB")
            candidate_print = Image.alpha_composite(background, candidate_tile).convert("RGB")
            difference = ImageChops.difference(source_print, candidate_print)
            try:
                histogram = difference.histogram()
                squared_error += sum(
                    (index % 256) ** 2 * count
                    for index, count in enumerate(histogram)
                )
                extrema = difference.getextrema()
                maximum = max(maximum, *(int(item[1]) for item in extrema))
            finally:
                difference.close()
                candidate_print.close()
                source_print.close()
                background.close()
                candidate_tile.close()
                source_tile.close()

    mse = squared_error / channel_count if channel_count else 0.0
    psnr_db = math.inf if mse == 0 else 10.0 * math.log10((255.0**2) / mse)
    return _PngQualityMetrics(
        psnr_db=psnr_db,
        max_channel_delta=maximum,
        alpha_exact=alpha_exact,
    )


def _copy_print_chunks(source_path: Path, png_path: Path, output_path: Path) -> None:
    source_chunks = [
        (chunk_type, data)
        for chunk_type, data in _read_raw_png_chunks(source_path)
        if chunk_type in PRINT_CHUNKS
    ]
    decoded_chunks = _read_raw_png_chunks(png_path)
    structural_chunks = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}
    with output_path.open("xb") as stream:
        stream.write(PNG_SIGNATURE)
        inserted = False
        for chunk_type, data in decoded_chunks:
            if chunk_type in PRINT_CHUNKS:
                continue
            if chunk_type not in structural_chunks:
                continue
            _write_png_chunk(stream, chunk_type, data)
            if chunk_type == b"IHDR" and not inserted:
                for source_type, source_data in source_chunks:
                    _write_png_chunk(stream, source_type, source_data)
                inserted = True


def _write_png_chunk(stream: Any, chunk_type: bytes, data: bytes) -> None:
    stream.write(len(data).to_bytes(4, "big"))
    stream.write(chunk_type)
    stream.write(data)
    stream.write((zlib.crc32(chunk_type + data) & 0xFFFFFFFF).to_bytes(4, "big"))


def _read_raw_png_chunks(path: Path) -> list[tuple[bytes, bytes]]:
    chunks: list[tuple[bytes, bytes]] = []
    with path.open("rb") as stream:
        if stream.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            raise RuntimeError(f"Sai PNG signature: {path.name}")
        while True:
            length_bytes = stream.read(4)
            if len(length_bytes) != 4:
                raise RuntimeError(f"PNG bị cắt ngắn: {path.name}")
            length = int.from_bytes(length_bytes, "big")
            chunk_type = stream.read(4)
            data = stream.read(length)
            crc = stream.read(4)
            if len(chunk_type) != 4 or len(data) != length or len(crc) != 4:
                raise RuntimeError(f"PNG bị cắt ngắn: {path.name}")
            expected = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
            if int.from_bytes(crc, "big") != expected:
                raise RuntimeError(f"PNG có CRC không hợp lệ: {path.name}")
            chunks.append((chunk_type, data))
            if chunk_type == b"IEND":
                return chunks


def _print_chunk_fingerprint(path: Path) -> tuple[tuple[str, Any], ...]:
    return tuple(
        item
        for item in _read_display_chunks(path)
        if item[0].encode("ascii") in PRINT_CHUNKS
    )


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
        optimized = optimize_png_for_pod(
            source,
            output,
            oxipng_executable=executable,
        )
        return PngProcessingResult(
            output_path=output,
            resized=False,
            compressed=True,
            input_bytes_for_compression=optimized.input_bytes,
            output_bytes=optimized.output_bytes,
            compression_mode=optimized.compression_mode,
            target_achieved=optimized.target_achieved,
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
        optimized = optimize_png_for_pod(
            staging,
            output,
            oxipng_executable=executable,
        )
        return PngProcessingResult(
            output_path=output,
            resized=True,
            compressed=True,
            input_bytes_for_compression=optimized.input_bytes,
            output_bytes=optimized.output_bytes,
            compression_mode=optimized.compression_mode,
            target_achieved=optimized.target_achieved,
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

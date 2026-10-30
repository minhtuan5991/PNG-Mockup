"""One sequential image-tools pipeline, composing the established processors."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tempfile
from typing import Callable, Iterable

from PIL import Image

from output_metadata import SUPPORTED_METADATA_IMAGE_EXTS, clean_image_metadata_copy
from output_watermark import WATERMARK_SIZE, create_watermarked_clean_copy, inspect_watermark
from png_optimizer import PngProcessingResult, process_png_file
from transparent_image_layout import LAYOUT_SPECS, OUTPUT_DPI, next_available_output_path


@dataclass(frozen=True)
class ToolImage:
    path: Path
    width: int
    height: int
    size_bytes: int


@dataclass(frozen=True)
class ToolOptions:
    layout: str | None = None
    compress: bool = False
    metadata: bool = False
    watermark: Path | None = None

    def validate(self) -> None:
        if not (self.layout or self.compress or self.metadata or self.watermark):
            raise ValueError("Hãy chọn ít nhất một tính năng để xử lý.")
        if self.layout is not None and self.layout not in LAYOUT_SPECS:
            raise ValueError("Hãy chọn kích thước 4500×5400 hoặc 4200×4800.")
        if self.watermark is not None:
            inspect_watermark(self.watermark)


@dataclass(frozen=True)
class ToolFileResult:
    source: Path
    output: Path
    compression: PngProcessingResult | None


@dataclass(frozen=True)
class ToolBatchResult:
    output_dir: Path
    outputs: tuple[ToolFileResult, ...]
    failures: tuple[tuple[Path, str], ...]


def inspect_tool_image(path: Path) -> ToolImage:
    """Read only the header for the gallery; processors validate full pixels."""
    source = path.resolve()
    if source.suffix.casefold() not in SUPPORTED_METADATA_IMAGE_EXTS:
        raise ValueError(f"Chỉ hỗ trợ PNG, JPG, JPEG và WebP: {source.name}")
    if not source.is_file():
        raise FileNotFoundError(f"Không tìm thấy tệp: {source}")
    with Image.open(source) as image:
        expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
        if image.format != expected[source.suffix.casefold()]:
            raise ValueError(f"Phần mở rộng không khớp định dạng ảnh: {source.name}")
        if int(getattr(image, "n_frames", 1)) != 1:
            raise ValueError(f"Ảnh động không được hỗ trợ: {source.name}")
        width, height = image.size
    return ToolImage(source, width, height, source.stat().st_size)


def validate_tool_inputs(images: Iterable[ToolImage], options: ToolOptions) -> None:
    options.validate()
    selected = list(images)
    if not selected:
        raise ValueError("Hãy chọn ít nhất một ảnh trong danh sách.")
    if options.layout or options.compress:
        non_png = [item.path.name for item in selected if item.path.suffix.casefold() != ".png"]
        if non_png:
            raise ValueError("Kích thước và Nén PNG chỉ hỗ trợ PNG. Bỏ chọn các tệp: " + ", ".join(non_png[:8]))
    if options.watermark:
        size = LAYOUT_SPECS[options.layout][:2] if options.layout else None
        mismatched = [item.path.name for item in selected if (size or (item.width, item.height)) != WATERMARK_SIZE]
        if mismatched:
            raise ValueError(
                "Watermark hiện yêu cầu ảnh kết quả 2000×2000 px. "
                "Bỏ chọn Kích thước hoặc chọn ảnh khớp Watermark: " + ", ".join(mismatched[:8])
            )


def create_tool_output_dir(parent: Path) -> Path:
    """Reserve a new result directory for every run, even in the same second."""
    name = "PNG Mockup - Đã xử lý " + datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    for index in range(1, 10000):
        output = parent / (name if index == 1 else f"{name} ({index})")
        try:
            output.mkdir()
            return output
        except FileExistsError:
            continue
    raise RuntimeError("Không tạo được thư mục kết quả mới.")


def process_tool_image(source: Path, destination: Path, options: ToolOptions) -> ToolFileResult:
    """Resize → watermark → compress → metadata, then publish one final copy.

    Nothing is written beside the user's source. Intermediates live in a unique
    directory beside the destination and are removed on success or failure.
    """
    options.validate()
    image = inspect_tool_image(source)
    validate_tool_inputs([image], options)
    source = image.path
    destination = destination.resolve()
    if source == destination or destination.exists():
        raise FileExistsError(f"File kết quả đã tồn tại: {destination}")
    if source.suffix.casefold() != destination.suffix.casefold():
        raise ValueError("File kết quả phải giữ nguyên phần mở rộng.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    compression: PngProcessingResult | None = None
    with tempfile.TemporaryDirectory(prefix=".png-mockup-processing-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        current = source
        # Preserve the fast combined resize/compression path when no overlay is
        # needed between the two operations.
        if options.layout or (options.compress and not options.watermark):
            current = stage / ("resized" + source.suffix)
            result = process_png_file(
                source, current, output_layout=options.layout,
                compress=options.compress and not bool(options.watermark), dpi=OUTPUT_DPI,
            )
            if result.compressed:
                compression = result
        if options.watermark:
            watermarked = stage / ("watermarked" + source.suffix)
            create_watermarked_clean_copy(current, watermarked, options.watermark)
            current = watermarked
        if options.compress and options.watermark:
            compressed = stage / ("compressed" + source.suffix)
            compression = process_png_file(current, compressed, output_layout=None, compress=True, dpi=OUTPUT_DPI)
            current = compressed
        if options.metadata and not options.watermark:
            cleaned = stage / ("cleaned" + source.suffix)
            clean_image_metadata_copy(current, cleaned)
            current = cleaned
        if current == source:
            raise ValueError("Chưa có tác vụ xử lý ảnh.")
        current.rename(destination)
    return ToolFileResult(source, destination, compression)


def run_tool_batch(
    sources: list[Path], options: ToolOptions,
    *, progress: Callable[[int, int, str], None] | None = None,
) -> ToolBatchResult:
    options.validate()
    if not sources:
        raise ValueError("Chưa chọn ảnh để xử lý.")
    # Validate the entire selection before reserving a result folder.
    images = [inspect_tool_image(path) for path in sources]
    validate_tool_inputs(images, options)
    output_dir = create_tool_output_dir(images[0].path.parent)
    outputs: list[ToolFileResult] = []
    failures: list[tuple[Path, str]] = []
    for index, image in enumerate(images, 1):
        if progress:
            progress(index - 1, len(images), f"Đang xử lý {index}/{len(images)}: {image.path.name}")
        try:
            destination = next_available_output_path(output_dir, image.path.name)
            outputs.append(process_tool_image(image.path, destination, options))
            status = f"Đã xong {index}/{len(images)}: {image.path.name}"
        except Exception as exc:
            failures.append((image.path, str(exc)))
            status = f"Lỗi {index}/{len(images)}: {image.path.name} — {exc}"
        if progress:
            progress(index, len(images), status)
    return ToolBatchResult(output_dir, tuple(outputs), tuple(failures))

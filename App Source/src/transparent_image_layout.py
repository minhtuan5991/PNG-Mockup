from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


SMALL_IMAGE_LONG_EDGE_PX = 2500
BASE_CANVAS_SIZE_PX = 4000
OUTPUT_DPI = 300
LAYOUT_SPECS = {
    "4500x5400": (4500, 5400, 4450, 5350),
    "4200x4800": (4200, 4800, 4150, 4750),
}


@dataclass(frozen=True)
class TransparentPngInfo:
    path: Path
    width: int
    height: int
    visible_bounds: tuple[int, int, int, int]

    @property
    def long_edge(self) -> int:
        return max(self.width, self.height)

    @property
    def is_small(self) -> bool:
        return self.long_edge < SMALL_IMAGE_LONG_EDGE_PX


def inspect_transparent_png(path: Path) -> TransparentPngInfo:
    source_path = path.resolve()
    if source_path.suffix.casefold() != ".png":
        raise ValueError(f"Chỉ hỗ trợ file PNG: {source_path.name}")
    if not source_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file: {source_path}")

    try:
        with Image.open(source_path) as source:
            if str(source.format or "").casefold() != "png":
                raise ValueError(f"File không phải PNG hợp lệ: {source_path.name}")
            width, height = source.size
            rgba = source.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ValueError(f"Không đọc được PNG {source_path.name}: {exc}") from exc

    try:
        alpha = rgba.getchannel("A")
        extrema = alpha.getextrema()
        clean_alpha = alpha.point(lambda value: 0 if value < 2 else value)
        bounds = clean_alpha.getbbox()
        clean_alpha.close()
        alpha.close()
    finally:
        rgba.close()

    if extrema == (255, 255):
        raise ValueError(f"PNG không có nền trong suốt: {source_path.name}")
    if extrema == (0, 0) or bounds is None:
        raise ValueError(f"PNG hoàn toàn trong suốt, không có nội dung: {source_path.name}")

    return TransparentPngInfo(
        path=source_path,
        width=width,
        height=height,
        visible_bounds=bounds,
    )


def next_available_output_path(output_dir: Path, source_name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    source = Path(source_name)
    candidate = output_dir / source.name
    if not candidate.exists():
        return candidate

    for index in range(2, 10000):
        candidate = output_dir / f"{source.stem}__{index}{source.suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Không tạo được tên output còn trống cho {source.name}")


def resize_transparent_png(
    source_path: Path,
    output_path: Path,
    output_layout: str,
    dpi: int = OUTPUT_DPI,
) -> Path:
    inspect_transparent_png(source_path)
    if output_layout not in LAYOUT_SPECS:
        raise ValueError(f"Kích thước output không hợp lệ: {output_layout}")

    with Image.open(source_path) as source:
        rgba = source.convert("RGBA")
        icc_profile = source.info.get("icc_profile")
    temporary = output_path.with_name(
        f".{output_path.stem}.{uuid.uuid4().hex}.tmp"
    )
    try:
        save_rgba_layout(
            rgba,
            temporary,
            base_size=BASE_CANVAS_SIZE_PX,
            dpi=dpi,
            output_layout=output_layout,
            icc_profile=icc_profile,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the no-overwrite promise even if another process creates this
        # path while the image is being rendered. On Windows, rename is atomic
        # within the directory and raises if the destination already exists.
        temporary.rename(output_path)
    finally:
        rgba.close()
        temporary.unlink(missing_ok=True)
    return output_path


def save_rgba_layout(
    image: Any,
    output_path: Path,
    base_size: int,
    dpi: int,
    output_layout: str = "4000x4000",
    icc_profile: bytes | None = None,
) -> None:
    if base_size <= 0:
        raise ValueError("Kích thước canvas cơ sở phải lớn hơn 0.")
    if dpi <= 0:
        raise ValueError("DPI phải lớn hơn 0.")
    if output_layout != "4000x4000" and output_layout not in LAYOUT_SPECS:
        raise ValueError(f"Kích thước output không hợp lệ: {output_layout}")

    source = image if getattr(image, "mode", None) == "RGBA" else image.convert("RGBA")
    source_is_copy = source is not image
    fitted = None
    base_canvas = None
    cropped = None
    layout_fitted = None
    canvas = None
    clean_alpha = None
    try:
        alpha = source.getchannel("A")
        extrema = alpha.getextrema()
        alpha.close()
        if extrema == (255, 255):
            raise RuntimeError("Ảnh PNG không có nền trong suốt.")
        if extrema == (0, 0):
            raise RuntimeError("Ảnh PNG hoàn toàn trong suốt.")

        fitted = ImageOps.contain(
            source,
            (base_size, base_size),
            Image.Resampling.LANCZOS,
        )
        base_canvas = Image.new("RGBA", (base_size, base_size), (0, 0, 0, 0))
        base_canvas.alpha_composite(
            fitted,
            ((base_size - fitted.width) // 2, (base_size - fitted.height) // 2),
        )
        canvas = base_canvas

        if output_layout in LAYOUT_SPECS:
            target_width, target_height, max_width, max_height = LAYOUT_SPECS[
                output_layout
            ]
            base_alpha = base_canvas.getchannel("A")
            pixel_bounds = base_alpha.getbbox()
            base_alpha.close()
            if pixel_bounds is None:
                raise RuntimeError("Ảnh không còn điểm ảnh hiển thị sau khi resize.")

            cropped = base_canvas.crop(pixel_bounds)
            layout_fitted = ImageOps.contain(
                cropped,
                (max_width, max_height),
                Image.Resampling.LANCZOS,
            )
            fitted_alpha = layout_fitted.getchannel("A").point(
                lambda value: 0 if value < 2 else value
            )
            fitted_bounds = fitted_alpha.getbbox()
            fitted_alpha.close()
            if fitted_bounds is None:
                raise RuntimeError("Ảnh không còn điểm ảnh hiển thị trong canvas đã chọn.")

            visible_width = fitted_bounds[2] - fitted_bounds[0]
            canvas = Image.new(
                "RGBA", (target_width, target_height), (0, 0, 0, 0)
            )
            canvas.alpha_composite(
                layout_fitted,
                (
                    (target_width - visible_width) // 2 - fitted_bounds[0],
                    25 - fitted_bounds[1],
                ),
            )

        clean_alpha = canvas.getchannel("A").point(
            lambda value: 0 if value < 2 else value
        )
        canvas.putalpha(clean_alpha)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        save_options: dict[str, Any] = {
            "format": "PNG",
            "dpi": (dpi, dpi),
            "optimize": True,
        }
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        canvas.save(output_path, **save_options)
    finally:
        if clean_alpha is not None:
            clean_alpha.close()
        if canvas is not None and canvas is not base_canvas:
            canvas.close()
        if layout_fitted is not None and layout_fitted is not cropped:
            layout_fitted.close()
        if cropped is not None:
            cropped.close()
        if base_canvas is not None:
            base_canvas.close()
        if fitted is not None and fitted is not source:
            fitted.close()
        if source_is_copy:
            source.close()

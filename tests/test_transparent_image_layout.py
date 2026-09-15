from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageOps


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "App Source" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import transparent_image_layout as layout


def _source_image(path: Path) -> None:
    image = Image.new("RGBA", (160, 120), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((28, 8, 132, 112), fill=(240, 90, 30, 255))
    draw.rectangle((55, 35, 105, 90), fill=(20, 110, 220, 170))
    image.save(path, format="PNG", dpi=(300, 300), icc_profile=b"pod-test-profile")
    image.close()


def _legacy_layout(source: Path, output: Path, spec: tuple[int, int, int, int]) -> None:
    target_width, target_height, max_width, max_height = spec
    with Image.open(source) as opened:
        rgba = opened.convert("RGBA")
        icc_profile = opened.info.get("icc_profile")
    fitted = ImageOps.contain(rgba, (400, 400), Image.Resampling.LANCZOS)
    base = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    base.alpha_composite(fitted, ((400 - fitted.width) // 2, (400 - fitted.height) // 2))
    alpha = base.getchannel("A")
    bounds = alpha.getbbox()
    alpha.close()
    assert bounds is not None
    cropped = base.crop(bounds)
    resized = ImageOps.contain(cropped, (max_width, max_height), Image.Resampling.LANCZOS)
    resized_alpha = resized.getchannel("A").point(lambda value: 0 if value < 2 else value)
    resized_bounds = resized_alpha.getbbox()
    resized_alpha.close()
    assert resized_bounds is not None
    visible_width = resized_bounds[2] - resized_bounds[0]
    canvas = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
    canvas.alpha_composite(
        resized,
        (
            (target_width - visible_width) // 2 - resized_bounds[0],
            25 - resized_bounds[1],
        ),
    )
    clean_alpha = canvas.getchannel("A").point(lambda value: 0 if value < 2 else value)
    canvas.putalpha(clean_alpha)
    canvas.save(
        output,
        format="PNG",
        dpi=(300, 300),
        optimize=True,
        icc_profile=icc_profile,
    )
    clean_alpha.close()
    canvas.close()
    resized.close()
    cropped.close()
    base.close()
    fitted.close()
    rgba.close()


class TransparentImageLayoutTests(unittest.TestCase):
    def test_fast_resize_matches_legacy_pixels_layout_and_print_metadata(self) -> None:
        spec = (450, 540, 445, 535)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.png"
            actual = folder / "actual.png"
            expected = folder / "expected.png"
            _source_image(source)

            with (
                patch.dict(layout.LAYOUT_SPECS, {"test-layout": spec}),
                patch.object(layout, "BASE_CANVAS_SIZE_PX", 400),
            ):
                layout.resize_transparent_png(source, actual, "test-layout", dpi=300)
            _legacy_layout(source, expected, spec)

            with Image.open(actual) as actual_image, Image.open(expected) as expected_image:
                actual_image.load()
                expected_image.load()
                self.assertEqual(actual_image.size, (450, 540))
                difference = ImageChops.difference(actual_image, expected_image)
                try:
                    self.assertIsNone(difference.getbbox(alpha_only=False))
                finally:
                    difference.close()
                self.assertEqual(
                    actual_image.info.get("icc_profile"), b"pod-test-profile"
                )
                self.assertAlmostEqual(actual_image.info["dpi"][0], 300, delta=0.02)
                alpha = actual_image.getchannel("A")
                try:
                    self.assertEqual(alpha.getbbox()[1], 25)
                finally:
                    alpha.close()

    def test_resize_opens_source_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.png"
            output = folder / "output.png"
            _source_image(source)
            real_open = Image.open
            with (
                patch.dict(layout.LAYOUT_SPECS, {"test-layout": (450, 540, 445, 535)}),
                patch.object(layout, "BASE_CANVAS_SIZE_PX", 400),
                patch.object(layout.Image, "open", wraps=real_open) as image_open,
            ):
                layout.resize_transparent_png(source, output, "test-layout")
            image_open.assert_called_once_with(source.resolve())

    def test_resize_uses_fast_lossless_png_encoding(self) -> None:
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        image.putpixel((8, 8), (10, 20, 30, 255))
        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output.png"
                with patch.object(Image.Image, "save") as save:
                    layout.save_rgba_layout(
                        image,
                        output,
                        base_size=16,
                        dpi=300,
                    )
                options = save.call_args.kwargs
                self.assertEqual(options["compress_level"], 3)
                self.assertNotIn("optimize", options)
        finally:
            image.close()


if __name__ == "__main__":
    unittest.main()

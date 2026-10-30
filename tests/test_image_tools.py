from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "App Source" / "src"))

import image_tools as tools
import transparent_image_layout as layout


def create_art(path: Path, size=(160, 180), *, compressed=True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGBA", size, (0, 0, 0, 0)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 15, size[0] - 20, size[1] - 15), fill=(220, 80, 40, 230))
        draw.ellipse((40, 40, 120, 140), fill=(40, 120, 200, 255))
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "original metadata")
        image.save(path, compress_level=6 if compressed else 0, dpi=(300, 300), icc_profile=b"print-profile", pnginfo=metadata)


class ImageToolsTests(unittest.TestCase):
    def test_combined_resize_compression_metadata_matches_existing_processors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Thiết kế.png"
            create_art(source)
            original = source.read_bytes()
            output = root / "result" / source.name
            expected = root / "expected.png"
            small_layout = (450, 540, 445, 535)
            with (
                patch.dict(layout.LAYOUT_SPECS, {"4500x5400": small_layout}),
                patch.object(layout, "BASE_CANVAS_SIZE_PX", 400),
            ):
                tools.process_png_file(source, expected, output_layout="4500x5400", compress=True, dpi=300)
                result = tools.process_tool_image(source, output, tools.ToolOptions(layout="4500x5400", compress=True, metadata=True))
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(result.compression.compressed)
            with Image.open(output) as actual, Image.open(expected) as baseline:
                self.assertEqual(actual.size, (450, 540))
                self.assertEqual(actual.convert("RGBA").tobytes(), baseline.convert("RGBA").tobytes())
                self.assertAlmostEqual(actual.info["dpi"][0], 300, delta=0.02)
                self.assertNotIn("icc_profile", actual.info)
                self.assertNotIn("Comment", actual.info)
            self.assertEqual([p.name for p in output.parent.iterdir()], [source.name])

    def test_batch_mixed_folders_duplicate_names_new_directory_and_sequential_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "A" / "a.png", root / "B" / "a.png"
            create_art(first)
            create_art(second)
            options = tools.ToolOptions(metadata=True)
            statuses = []
            result = tools.run_tool_batch([first, second], options, progress=lambda done, total, text: statuses.append((done, total, text)))
            self.assertEqual(result.output_dir.parent, first.parent)
            self.assertEqual([item.output.name for item in result.outputs], ["a.png", "a__2.png"])
            self.assertFalse(result.failures)
            self.assertEqual([done for done, _, _ in statuses], [0, 1, 1, 2])
            next_run = tools.run_tool_batch([first], options)
            self.assertNotEqual(result.output_dir, next_run.output_dir)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_watermark_then_compression_preserves_overlay_and_required_metadata_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            create_art(source, (2000, 2000), compressed=False)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            watermark = root / "mark.png"
            with Image.new("RGBA", (2000, 2000), (0, 0, 0, 0)) as image:
                ImageDraw.Draw(image).rectangle((0, 0, 100, 100), fill=(0, 255, 0, 255))
                image.save(watermark, dpi=(72, 72))
            output = root / "result.png"
            calls = []
            real_mark, real_process = tools.create_watermarked_clean_copy, tools.process_png_file

            def mark(*args, **kwargs):
                calls.append("watermark")
                return real_mark(*args, **kwargs)

            def compress(*args, **kwargs):
                calls.append("compress")
                return real_process(*args, **kwargs)

            with (
                patch.object(tools, "create_watermarked_clean_copy", side_effect=mark),
                patch.object(tools, "process_png_file", side_effect=compress),
            ):
                tools.process_tool_image(source, output, tools.ToolOptions(watermark=watermark, compress=True))
            self.assertEqual(calls, ["watermark", "compress"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            with Image.open(output) as image:
                self.assertEqual(image.size, (2000, 2000))
                rgba = image.convert("RGBA")
                self.assertEqual(rgba.getpixel((20, 20)), (0, 255, 0, 255))
                rgba.close()
                self.assertNotIn("icc_profile", image.info)
                self.assertNotIn("Comment", image.info)
                self.assertAlmostEqual(image.info["dpi"][0], 300, delta=0.02)

    def test_metadata_only_keeps_jpeg_and_webp_support_without_reencoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for extension in ("jpg", "webp"):
                with self.subTest(extension=extension):
                    source = root / f"source.{extension}"
                    with Image.new("RGB", (40, 32), (30, 90, 170)) as image:
                        image.save(source, icc_profile=b"print-profile")
                    original = source.read_bytes()
                    result = tools.run_tool_batch([source], tools.ToolOptions(metadata=True))
                    self.assertFalse(result.failures)
                    with Image.open(source) as before, Image.open(result.outputs[0].output) as after:
                        self.assertEqual(before.tobytes(), after.tobytes())
                        self.assertNotIn("icc_profile", after.info)
                    self.assertEqual(source.read_bytes(), original)

    def test_incompatible_operations_are_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.jpg"
            with Image.new("RGB", (16, 16)) as image:
                image.save(source)
            with self.assertRaisesRegex(ValueError, "chỉ hỗ trợ PNG"):
                tools.run_tool_batch([source], tools.ToolOptions(compress=True))
            self.assertEqual(list(root.iterdir()), [source])
            with self.assertRaisesRegex(ValueError, "ít nhất một tính năng"):
                tools.run_tool_batch([source], tools.ToolOptions())

    def test_watermark_size_rule_is_preserved_for_large_resize_canvas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            create_art(source)
            watermark = root / "watermark.png"
            with Image.new("RGBA", (2000, 2000), (30, 100, 150, 180)) as image:
                image.save(watermark, dpi=(72, 72))
            with self.assertRaisesRegex(ValueError, "2000×2000"):
                tools.run_tool_batch([source], tools.ToolOptions(layout="4500x5400", watermark=watermark))
            self.assertEqual(set(root.iterdir()), {source, watermark})

    def test_failure_cleans_staging_and_batch_continues_without_touching_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opaque, valid = root / "opaque.png", root / "valid.png"
            with Image.new("RGB", (30, 30), (255, 0, 0)) as image:
                image.save(opaque)
            create_art(valid)
            original = valid.read_bytes()
            with (
                patch.dict(layout.LAYOUT_SPECS, {"4500x5400": (450, 540, 445, 535)}),
                patch.object(layout, "BASE_CANVAS_SIZE_PX", 400),
            ):
                result = tools.run_tool_batch([opaque, valid], tools.ToolOptions(layout="4500x5400"))
            self.assertEqual(len(result.outputs), 1)
            self.assertEqual(len(result.failures), 1)
            self.assertEqual(result.outputs[0].source, valid)
            self.assertEqual(valid.read_bytes(), original)
            self.assertEqual([p.name for p in result.output_dir.iterdir()], [valid.name])
            destination = root / "failed" / valid.name

            def fail_copy(_source, output):
                output.write_bytes(b"partial")
                raise OSError("simulated write failure")

            with patch.object(tools, "clean_image_metadata_copy", side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, "simulated"):
                    tools.process_tool_image(valid, destination, tools.ToolOptions(metadata=True))
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

import output_watermark  # noqa: E402
import psd_mockup  # noqa: E402
from config import app_temp_root, load_config  # noqa: E402
from output_metadata import OutputMetadataError  # noqa: E402
from output_watermark import (  # noqa: E402
    OutputWatermarkError,
    apply_watermark_atomic,
    create_watermarked_clean_copy,
    inspect_watermark,
    watermark_output_folder_name,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REMOVED_PNG_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"iCCP"}


def png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", checksum)
    )


def parse_png_chunk_types(path: Path) -> list[bytes]:
    raw = path.read_bytes()
    if not raw.startswith(PNG_SIGNATURE):
        raise AssertionError("Test image is not PNG")
    types: list[bytes] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        types.append(chunk_type)
        offset += 12 + length
        if chunk_type == b"IEND":
            break
    return types


def add_all_requested_png_metadata(path: Path) -> None:
    raw = path.read_bytes()
    insert_at = raw.index(b"IDAT") - 4
    metadata = b"".join(
        (
            png_chunk(b"tEXt", b"Comment\x00private comment"),
            png_chunk(
                b"zTXt",
                b"IPTC\x00\x00" + zlib.compress(b"private IPTC"),
            ),
            png_chunk(
                b"iTXt",
                b"XML:com.adobe.xmp\x00\x00\x00\x00\x00"
                b"<x:xmpmeta>private XMP</x:xmpmeta>",
            ),
            png_chunk(b"eXIf", b"Exif\x00\x00thumbnail and EXIF"),
            png_chunk(b"iCCP", b"ICC Profile\x00\x00compressed profile"),
        )
    )
    path.write_bytes(raw[:insert_at] + metadata + raw[insert_at:])


def create_watermark(
    path: Path,
    size: tuple[int, int] = (16, 16),
    *,
    dpi: int | None = 72,
    alpha: int = 128,
) -> None:
    image = Image.new("RGBA", size, (255, 0, 0, alpha))
    save_kwargs = {"dpi": (dpi, dpi)} if dpi is not None else {}
    image.save(path, format="PNG", **save_kwargs)


class WatermarkValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        output_watermark.clear_watermark_cache()

    def tearDown(self) -> None:
        output_watermark.clear_watermark_cache()
        self.temporary.cleanup()

    def test_valid_transparent_png_accepts_png_dpi_rounding(self) -> None:
        watermark = self.root / "Logo.png"
        create_watermark(watermark)

        info = inspect_watermark(
            watermark,
            expected_size=(16, 16),
            expected_dpi=72,
        )

        self.assertEqual((info.width, info.height), (16, 16))
        self.assertTrue(info.has_transparency)
        self.assertAlmostEqual(info.dpi_x, 72, delta=1)
        self.assertAlmostEqual(info.dpi_y, 72, delta=1)

    def test_rejects_wrong_container_size_dpi_and_transparency(self) -> None:
        fake_png = self.root / "fake.png"
        Image.new("RGB", (16, 16), "white").save(fake_png, format="JPEG")
        with self.assertRaisesRegex(OutputWatermarkError, "không phải PNG"):
            inspect_watermark(
                fake_png,
                expected_size=(16, 16),
                expected_dpi=72,
            )

        wrong_size = self.root / "wrong-size.png"
        create_watermark(wrong_size, size=(12, 16))
        with self.assertRaisesRegex(OutputWatermarkError, "16x16"):
            inspect_watermark(
                wrong_size,
                expected_size=(16, 16),
                expected_dpi=72,
            )

        wrong_dpi = self.root / "wrong-dpi.png"
        create_watermark(wrong_dpi, dpi=300)
        with self.assertRaisesRegex(OutputWatermarkError, "72 DPI"):
            inspect_watermark(
                wrong_dpi,
                expected_size=(16, 16),
                expected_dpi=72,
            )

        opaque = self.root / "opaque.png"
        create_watermark(opaque, alpha=255)
        with self.assertRaisesRegex(OutputWatermarkError, "nền trong suốt"):
            inspect_watermark(
                opaque,
                expected_size=(16, 16),
                expected_dpi=72,
            )

    def test_rejects_animated_png_watermark(self) -> None:
        animated = self.root / "animated.png"
        first = Image.new("RGBA", (16, 16), (255, 0, 0, 128))
        second = Image.new("RGBA", (16, 16), (0, 255, 0, 128))
        first.save(
            animated,
            format="PNG",
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
            dpi=(72, 72),
        )

        with self.assertRaisesRegex(OutputWatermarkError, "PNG động/APNG"):
            inspect_watermark(
                animated,
                expected_size=(16, 16),
                expected_dpi=72,
            )

    def test_atomic_watermark_changes_pixels_and_preserves_canvas(self) -> None:
        source = self.root / "mockup.png"
        watermark = self.root / "Logo.png"
        Image.new("RGB", (16, 16), (0, 0, 255)).save(
            source,
            format="PNG",
            dpi=(72, 72),
        )
        create_watermark(watermark)

        result = apply_watermark_atomic(
            source,
            watermark,
            expected_size=(16, 16),
            expected_dpi=72,
        )

        self.assertEqual(result.output_path, source)
        with Image.open(source) as image:
            image.load()
            self.assertEqual(image.size, (16, 16))
            self.assertEqual(image.mode, "RGB")
            self.assertAlmostEqual(image.info["dpi"][0], 72, delta=1)
            self.assertAlmostEqual(image.info["dpi"][1], 72, delta=1)
            red, green, blue = image.getpixel((0, 0))
            self.assertGreater(red, 120)
            self.assertEqual(green, 0)
            self.assertGreater(blue, 120)
        self.assertFalse(list(self.root.glob(".*.watermark-*.png")))

    def test_manual_watermark_then_removes_all_six_metadata_groups(self) -> None:
        source = self.root / "mockup.png"
        watermark = self.root / "Logo.png"
        output_dir = self.root / watermark_output_folder_name(watermark)
        output = output_dir / "mockup.cleaned.png"
        Image.new("RGB", (2000, 2000), (0, 80, 160)).save(
            source,
            format="PNG",
            dpi=(72, 72),
            compress_level=1,
        )
        add_all_requested_png_metadata(source)
        create_watermark(watermark, size=(2000, 2000), alpha=96)
        original_source = source.read_bytes()

        create_watermarked_clean_copy(source, output, watermark)

        self.assertEqual(source.read_bytes(), original_source)
        self.assertTrue(output.is_file())
        output_types = set(parse_png_chunk_types(output))
        self.assertTrue(REMOVED_PNG_CHUNKS.isdisjoint(output_types))
        self.assertIn(b"pHYs", output_types)
        with Image.open(output) as image:
            image.load()
            self.assertEqual(image.size, (2000, 2000))
            self.assertEqual(image.mode, "RGB")
            self.assertAlmostEqual(image.info["dpi"][0], 72, delta=1)
            self.assertAlmostEqual(image.info["dpi"][1], 72, delta=1)
        self.assertFalse(list(output_dir.glob(".*.watermark-*")))
        self.assertFalse(list(output_dir.glob(".*.metadata-*")))

    def test_watermark_folder_name_uses_file_stem_safely(self) -> None:
        self.assertEqual(watermark_output_folder_name("Logo.png"), "Logo")
        self.assertEqual(
            watermark_output_folder_name("bad:name?.png"),
            "bad_name_",
        )
        self.assertEqual(watermark_output_folder_name("CON.png"), "_CON")


class WatermarkPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()




    def test_psd_pipeline_applies_watermark_before_metadata(self) -> None:
        project_dir = self.root / "app"
        output_dir = project_dir / "Output"
        logs_dir = project_dir / "logs"
        output_dir.mkdir(parents=True)
        logs_dir.mkdir()
        mockup_path = project_dir / "shirt.1S.psd"
        design_path = project_dir / "design.png"
        mockup_path.write_bytes(b"psd")
        design_path.write_bytes(b"design")
        config = SimpleNamespace(
            project_root=project_dir,
            output_dir=output_dir,
            logs_dir=logs_dir,
            generation_timeout_sec=10,
            output_watermark_enabled=True,
            output_watermark_path=self.root / "Logo.png",
            output_width_px=2000,
            output_height_px=2000,
            output_dpi=72,
            remove_output_metadata=True,
        )
        job = {
            "id": "000001",
            "output_name": "design__shirt__PSD_JOB.png",
            "psd_output_prefix": "design__shirt",
            "mockup_path": str(mockup_path),
            "psd_replacements": [
                {"path": str(design_path), "layer": "Design Dark"}
            ],
        }
        calls: list[str] = []

        def fake_photoshop_run(
            *_args: object,
            **_kwargs: object,
        ) -> SimpleNamespace:
            work_dir = (
                app_temp_root(project_dir)
                / "psd_jobs"
                / "design__shirt__PSD_JOB"
            )
            spec = json.loads(
                (work_dir / "job.json").read_text(encoding="utf-8")
            )
            export_path = Path(spec["export_dir"]) / "design__shirt__blue.png"
            export_path.write_bytes(b"photoshop png")
            Path(spec["result_path"]).write_text(
                json.dumps({"ok": True, "outputs": [str(export_path)]}),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="")

        with (
            patch.object(psd_mockup, "find_photoshop_exe", return_value=Path("Photoshop.exe")),
            patch.object(psd_mockup.tempfile, "gettempdir", return_value=str(self.root)),
            patch.object(psd_mockup.subprocess, "run", side_effect=fake_photoshop_run),
            patch.object(
                psd_mockup,
                "apply_watermark_atomic",
                side_effect=lambda *_args, **_kwargs: calls.append("watermark"),
            ),
            patch.object(
                psd_mockup,
                "strip_png_metadata_atomic",
                side_effect=lambda *_args, **_kwargs: calls.append("metadata"),
            ),
        ):
            psd_mockup.run_psd_job(config, job, MagicMock())

        self.assertEqual(calls, ["watermark", "metadata"])
        exported = output_dir / "design__shirt__blue.png"
        self.assertEqual(exported.read_bytes(), b"photoshop png")

        # The same Photoshop result must never replace a verified Output file
        # if either local postprocessing step fails. No second render is made.
        for failing_step, error in (
            ("watermark", OutputWatermarkError("invalid watermark")),
            ("metadata", OutputMetadataError("metadata validation failed")),
        ):
            with self.subTest(failing_step=failing_step):
                exported.write_bytes(b"existing verified output")
                with (
                    patch.object(psd_mockup, "find_photoshop_exe", return_value=Path("Photoshop.exe")),
                    patch.object(psd_mockup.tempfile, "gettempdir", return_value=str(self.root)),
                    patch.object(psd_mockup.subprocess, "run", side_effect=fake_photoshop_run) as render,
                    patch.object(psd_mockup, "apply_watermark_atomic", side_effect=error if failing_step == "watermark" else None),
                    patch.object(psd_mockup, "strip_png_metadata_atomic", side_effect=error if failing_step == "metadata" else None),
                ):
                    with self.assertRaises(type(error)):
                        psd_mockup.run_psd_job(config, job, MagicMock())
                render.assert_called_once()
                self.assertEqual(exported.read_bytes(), b"existing verified output")
                self.assertFalse(list(output_dir.glob(".*.postprocess-*")))



class WatermarkConfigTests(unittest.TestCase):
    def test_old_config_defaults_to_watermark_off_with_no_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text("{}", encoding="utf-8")

            config = load_config(config_path)

        self.assertFalse(config.output_watermark_enabled)
        self.assertIsNone(config.output_watermark_path)


if __name__ == "__main__":
    unittest.main()

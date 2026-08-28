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

from PIL import Image, features


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

import output_metadata  # noqa: E402
import psd_mockup  # noqa: E402
from config import app_temp_root, load_config  # noqa: E402
from output_metadata import (  # noqa: E402
    OutputMetadataError,
    PNG_METADATA_CHUNKS,
    clean_image_metadata_copy,
    cleaned_image_filename,
    strip_png_metadata_atomic,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", checksum)
    )


def parse_png_chunks(path: Path) -> list[tuple[bytes, bytes, bytes]]:
    raw = path.read_bytes()
    if not raw.startswith(PNG_SIGNATURE):
        raise AssertionError("Test image is not PNG")
    chunks: list[tuple[bytes, bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        crc = raw[payload_end : payload_end + 4]
        chunks.append((chunk_type, raw[payload_start:payload_end], crc))
        offset = payload_end + 4
        if chunk_type == b"IEND":
            break
    return chunks


def add_requested_metadata(path: Path) -> None:
    chunks = parse_png_chunks(path)
    metadata = [
        png_chunk(b"tEXt", b"Comment\x00private comment"),
        png_chunk(
            b"zTXt",
            b"IPTC\x00\x00" + zlib.compress(b"private compressed IPTC"),
        ),
        png_chunk(
            b"iTXt",
            b"XML:com.adobe.xmp\x00\x00\x00\x00\x00"
            + b"<x:xmpmeta>private XMP</x:xmpmeta>",
        ),
        png_chunk(
            b"iTXt",
            b"IPTC\x00\x00\x00\x00\x00private UTF-8 IPTC",
        ),
        png_chunk(
            b"eXIf",
            b"MM\x00*\x00\x00\x00\x08\x00\x00\x00\x00",
        ),
        png_chunk(
            b"iCCP",
            b"ICC Profile\x00\x00" + zlib.compress(b"private fake ICC profile"),
        ),
    ]

    rebuilt = bytearray(PNG_SIGNATURE)
    inserted = False
    for chunk_type, payload, crc in chunks:
        if chunk_type == b"IDAT" and not inserted:
            for encoded in metadata:
                rebuilt.extend(encoded)
            inserted = True
        rebuilt.extend(struct.pack(">I", len(payload)))
        rebuilt.extend(chunk_type)
        rebuilt.extend(payload)
        rebuilt.extend(crc)
    path.write_bytes(bytes(rebuilt))


def create_rgba_png(path: Path, with_metadata: bool = True) -> None:
    image = Image.new("RGBA", (7, 5), (40, 80, 120, 0))
    image.putpixel((2, 3), (200, 30, 90, 255))
    image.save(path, format="PNG", dpi=(300, 300), compress_level=1)
    image.close()
    if with_metadata:
        add_requested_metadata(path)


def chunk_payloads(path: Path, chunk_type: bytes) -> tuple[bytes, ...]:
    return tuple(
        payload
        for current_type, payload, _ in parse_png_chunks(path)
        if current_type == chunk_type
    )


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes((marker,)) + struct.pack(">H", len(payload) + 2) + payload


def add_requested_jpeg_metadata(path: Path) -> None:
    raw = path.read_bytes()
    if not raw.startswith(b"\xff\xd8"):
        raise AssertionError("Test image is not JPEG")
    metadata = b"".join(
        (
            jpeg_segment(0xFE, b"private comment"),
            jpeg_segment(0xE1, b"Exif\x00\x00private EXIF and thumbnail"),
            jpeg_segment(
                0xE1,
                b"http://ns.adobe.com/xap/1.0/\x00private XMP",
            ),
            jpeg_segment(0xED, b"Photoshop 3.0\x00private IPTC"),
            jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01private ICC profile"),
        )
    )
    path.write_bytes(raw[:2] + metadata + raw[2:])


def jpeg_scan_section(path: Path) -> bytes:
    raw = path.read_bytes()
    if not raw.startswith(b"\xff\xd8"):
        raise AssertionError("Test image is not JPEG")
    offset = 2
    while offset < len(raw):
        marker_start = offset
        if raw[offset] != 0xFF:
            raise AssertionError("Invalid JPEG marker")
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        marker = raw[offset]
        offset += 1
        if marker == 0xDA:
            return raw[marker_start:]
        if marker in {0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)}:
            continue
        segment_length = struct.unpack(">H", raw[offset : offset + 2])[0]
        offset += segment_length
    raise AssertionError("JPEG has no scan section")


def jpeg_marker_after_first_scan(raw: bytes) -> int:
    offset = 2
    while offset < len(raw):
        marker_start = offset
        if raw[offset] != 0xFF:
            raise AssertionError("Invalid JPEG marker")
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        marker = raw[offset]
        offset += 1
        if marker == 0xDA:
            segment_length = struct.unpack(">H", raw[offset : offset + 2])[0]
            offset += segment_length
            while offset < len(raw) - 1:
                marker_start = raw.find(b"\xff", offset)
                if marker_start < 0:
                    raise AssertionError("JPEG has no marker after scan")
                marker = raw[marker_start + 1]
                if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                    offset = marker_start + 2
                    continue
                if marker == 0xFF:
                    offset = marker_start + 1
                    continue
                return marker_start
            raise AssertionError("JPEG has no marker after scan")
        if marker in {0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)}:
            continue
        segment_length = struct.unpack(">H", raw[offset : offset + 2])[0]
        offset += segment_length
    raise AssertionError("JPEG has no SOS marker")


class OutputMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_removes_requested_metadata_without_recompressing_pixels(self) -> None:
        image_path = self.root / "mockup.png"
        create_rgba_png(image_path)

        before_idat = chunk_payloads(image_path, b"IDAT")
        before_phys = chunk_payloads(image_path, b"pHYs")
        with Image.open(image_path) as image:
            before_size = image.size
            before_pixels = image.convert("RGBA").tobytes()

        result = strip_png_metadata_atomic(image_path)

        chunk_types = {chunk_type for chunk_type, _, _ in parse_png_chunks(image_path)}
        self.assertTrue(PNG_METADATA_CHUNKS.isdisjoint(chunk_types))
        self.assertEqual(chunk_payloads(image_path, b"IDAT"), before_idat)
        self.assertEqual(chunk_payloads(image_path, b"pHYs"), before_phys)
        with Image.open(image_path) as image:
            self.assertEqual(image.size, before_size)
            self.assertEqual(image.convert("RGBA").tobytes(), before_pixels)
            self.assertTrue("A" in image.getbands())
        with Image.open(image_path) as image:
            image.verify()

        self.assertTrue(result.replaced)
        self.assertEqual(
            set(result.removed_chunks),
            {"tEXt", "zTXt", "iTXt", "eXIf", "iCCP"},
        )
        self.assertFalse(list(self.root.glob(".*.metadata-*.tmp")))

    def test_validation_failure_keeps_original_and_removes_temporary_file(self) -> None:
        image_path = self.root / "keep-original.png"
        create_rgba_png(image_path)
        original_bytes = image_path.read_bytes()

        with patch.object(
            output_metadata,
            "_verify_png_can_open",
            side_effect=OutputMetadataError("forced validation failure"),
        ):
            with self.assertRaises(OutputMetadataError):
                strip_png_metadata_atomic(image_path)

        self.assertEqual(image_path.read_bytes(), original_bytes)
        self.assertFalse(list(self.root.glob(".*.metadata-*.tmp")))

    def test_replace_failure_keeps_original_and_removes_temporary_file(self) -> None:
        image_path = self.root / "replace-denied.png"
        create_rgba_png(image_path)
        original_bytes = image_path.read_bytes()

        with patch.object(
            output_metadata.os,
            "replace",
            side_effect=PermissionError("forced replace failure"),
        ):
            with self.assertRaises(OutputMetadataError):
                strip_png_metadata_atomic(image_path)

        self.assertEqual(image_path.read_bytes(), original_bytes)
        self.assertFalse(list(self.root.glob(".*.metadata-*.tmp")))

    def test_palette_transparency_chunk_is_preserved(self) -> None:
        image_path = self.root / "palette-alpha.png"
        image = Image.new("P", (6, 4), 0)
        image.putpalette([0, 0, 0, 255, 30, 60] + [0, 0, 0] * 254)
        image.putpixel((2, 2), 1)
        image.save(
            image_path,
            format="PNG",
            transparency=0,
            dpi=(300, 300),
        )
        image.close()
        add_requested_metadata(image_path)
        before_trns = chunk_payloads(image_path, b"tRNS")

        strip_png_metadata_atomic(image_path)

        self.assertEqual(chunk_payloads(image_path, b"tRNS"), before_trns)
        with Image.open(image_path) as cleaned:
            self.assertEqual(cleaned.size, (6, 4))
            self.assertIn("transparency", cleaned.info)
            cleaned.verify()

    def test_invalid_png_is_rejected_without_touching_source(self) -> None:
        image_path = self.root / "invalid.png"
        image_path.write_bytes(PNG_SIGNATURE + b"truncated")
        original_bytes = image_path.read_bytes()

        with self.assertRaises(OutputMetadataError):
            strip_png_metadata_atomic(image_path)

        self.assertEqual(image_path.read_bytes(), original_bytes)
        self.assertFalse(list(self.root.glob(".*.metadata-*.tmp")))

    def test_clean_png_is_a_fast_noop(self) -> None:
        image_path = self.root / "already-clean.png"
        create_rgba_png(image_path, with_metadata=False)
        original_bytes = image_path.read_bytes()

        with patch.object(output_metadata.os, "replace") as replace:
            result = strip_png_metadata_atomic(image_path)

        replace.assert_not_called()
        self.assertFalse(result.replaced)
        self.assertEqual(image_path.read_bytes(), original_bytes)


    def test_psd_job_only_processes_paths_exported_by_that_job(self) -> None:
        project_dir = self.root / "app"
        output_dir = project_dir / "Output"
        logs_dir = project_dir / "logs"
        output_dir.mkdir(parents=True)
        logs_dir.mkdir()
        mockup_path = project_dir / "shirt.1S.psd"
        design_path = project_dir / "design.png"
        mockup_path.write_bytes(b"psd")
        create_rgba_png(design_path, with_metadata=False)
        decoy = output_dir / "old-output.png"
        create_rgba_png(decoy)
        decoy_bytes = decoy.read_bytes()
        config = SimpleNamespace(
            project_root=project_dir,
            output_dir=output_dir,
            logs_dir=logs_dir,
            generation_timeout_sec=10,
            remove_output_metadata=True,
        )
        job = {
            "id": "000001",
            "output_name": "design__shirt__PSD_JOB.png",
            "psd_output_prefix": "design__shirt",
            "mockup_path": str(mockup_path),
            "psd_replacements": [{"path": str(design_path), "layer": "Design Dark"}],
        }

        def fake_photoshop_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
            work_dir = (
                app_temp_root(project_dir)
                / "psd_jobs"
                / "design__shirt__PSD_JOB"
            )
            spec = json.loads((work_dir / "job.json").read_text(encoding="utf-8"))
            export_path = Path(spec["export_dir"]) / "design__shirt__blue.png"
            create_rgba_png(export_path)
            Path(spec["result_path"]).write_text(
                json.dumps({"ok": True, "outputs": [str(export_path)]}),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="")

        with (
            patch.object(psd_mockup.tempfile, "gettempdir", return_value=str(self.root)),
            patch.object(psd_mockup, "find_photoshop_exe", return_value=Path("Photoshop.exe")),
            patch.object(psd_mockup.subprocess, "run", side_effect=fake_photoshop_run),
        ):
            psd_mockup.run_psd_job(config, job, MagicMock())

        exported = Path(job["psd_exported_paths"][0])
        exported_types = {chunk_type for chunk_type, _, _ in parse_png_chunks(exported)}
        self.assertTrue(PNG_METADATA_CHUNKS.isdisjoint(exported_types))
        self.assertEqual(decoy.read_bytes(), decoy_bytes)

    def test_config_defaults_on_and_accepts_explicit_false(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        self.assertTrue(load_config(config_path).remove_output_metadata)
        config_path.write_text(
            json.dumps({"remove_output_metadata": False}),
            encoding="utf-8",
        )
        self.assertFalse(load_config(config_path).remove_output_metadata)


class ManualMetadataCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.done = self.root / "Done"

    def test_png_copy_preserves_source_and_pixel_chunks(self) -> None:
        source = self.root / "mockup.png"
        output = self.done / "mockup.cleaned.png"
        create_rgba_png(source)
        source_bytes = source.read_bytes()
        source_idat = chunk_payloads(source, b"IDAT")
        source_phys = chunk_payloads(source, b"pHYs")

        result = clean_image_metadata_copy(source, output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(result.source_path, source.resolve())
        self.assertEqual(result.output_path, output.resolve())
        self.assertEqual(chunk_payloads(output, b"IDAT"), source_idat)
        self.assertEqual(chunk_payloads(output, b"pHYs"), source_phys)
        output_types = {chunk_type for chunk_type, _, _ in parse_png_chunks(output)}
        self.assertTrue(PNG_METADATA_CHUNKS.isdisjoint(output_types))
        with Image.open(output) as cleaned:
            self.assertEqual(cleaned.size, (7, 5))
            self.assertIn("A", cleaned.getbands())
            cleaned.verify()

    def test_cleaned_filename_inserts_suffix_before_extension(self) -> None:
        self.assertEqual(
            cleaned_image_filename("Áo Mockup.V1.PNG"),
            "Áo Mockup.V1.cleaned.PNG",
        )
        with self.assertRaises(OutputMetadataError):
            cleaned_image_filename("notes.txt")

    def test_jpeg_copy_removes_requested_sections_without_recompression(self) -> None:
        source = self.root / "photo.jpg"
        output = self.done / "photo.cleaned.jpg"
        image = Image.new("RGB", (11, 9), (50, 90, 130))
        image.save(source, format="JPEG", quality=91)
        image.close()
        add_requested_jpeg_metadata(source)
        source_bytes = source.read_bytes()
        source_scan = jpeg_scan_section(source)

        result = clean_image_metadata_copy(source, output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(output_metadata._inspect_jpeg_metadata(output), ())
        self.assertEqual(jpeg_scan_section(output), source_scan)
        self.assertEqual(
            set(result.removed_sections),
            {"Comment", "EXIF", "XMP", "IPTC", "ICC profile"},
        )
        with Image.open(output) as cleaned:
            self.assertEqual(cleaned.size, (11, 9))
            cleaned.verify()

    def test_jpeg_metadata_between_progressive_scans_is_removed(self) -> None:
        source = self.root / "progressive.jpg"
        output = self.done / "progressive.cleaned.jpg"
        image = Image.new("RGB", (32, 24), (60, 100, 140))
        image.putpixel((8, 7), (220, 30, 90))
        image.save(source, format="JPEG", quality=92, progressive=True)
        image.close()
        raw = source.read_bytes()
        insertion_offset = jpeg_marker_after_first_scan(raw)
        source.write_bytes(
            raw[:insertion_offset]
            + jpeg_segment(0xE1, b"Exif\x00\x00private inter-scan EXIF")
            + raw[insertion_offset:]
        )
        with Image.open(source) as original:
            original_pixels = original.convert("RGB").tobytes()

        result = clean_image_metadata_copy(source, output)

        self.assertIn("EXIF", result.removed_sections)
        self.assertEqual(output_metadata._inspect_jpeg_metadata(output), ())
        with Image.open(output) as cleaned:
            self.assertEqual(cleaned.convert("RGB").tobytes(), original_pixels)

    def test_jpeg_trailing_payload_is_discarded_after_eoi(self) -> None:
        source = self.root / "trailing.jpg"
        output = self.done / "trailing.cleaned.jpg"
        image = Image.new("RGB", (12, 10), (10, 20, 30))
        image.save(source, format="JPEG")
        image.close()
        source.write_bytes(
            source.read_bytes()
            + jpeg_segment(0xFE, b"private-comment-after-eoi")
            + b"hidden trailing bytes"
        )
        source_bytes = source.read_bytes()

        result = clean_image_metadata_copy(source, output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertIn("Trailing data", result.removed_sections)
        self.assertTrue(output.read_bytes().endswith(b"\xff\xd9"))
        self.assertNotIn(b"private-comment-after-eoi", output.read_bytes())
        self.assertEqual(output_metadata._inspect_jpeg_metadata(output), ())

    def test_concatenated_jpeg_is_rejected_instead_of_dropping_second_image(
        self,
    ) -> None:
        source = self.root / "two-images.jpg"
        output = self.done / "two-images.cleaned.jpg"
        first = Image.new("RGB", (12, 10), (10, 20, 30))
        second_path = self.root / "second.jpg"
        second = Image.new("RGB", (8, 6), (40, 50, 60))
        first.save(source, format="JPEG")
        second.save(second_path, format="JPEG")
        first.close()
        second.close()
        source.write_bytes(source.read_bytes() + second_path.read_bytes())
        source_bytes = source.read_bytes()

        with self.assertRaises(OutputMetadataError):
            clean_image_metadata_copy(source, output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertFalse(output.exists())

    def test_truncated_jpeg_is_rejected_before_output_is_published(self) -> None:
        source = self.root / "truncated.jpg"
        output = self.done / "truncated.cleaned.jpg"
        image = Image.new("RGB", (18, 14), (80, 120, 160))
        image.save(source, format="JPEG")
        image.close()
        source.write_bytes(source.read_bytes()[:-2])
        source_bytes = source.read_bytes()

        with self.assertRaises(OutputMetadataError):
            clean_image_metadata_copy(source, output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertFalse(output.exists())

    @unittest.skipUnless(features.check("webp"), "Pillow WebP support is unavailable")
    def test_webp_copy_removes_metadata_and_preserves_source(self) -> None:
        source = self.root / "art.webp"
        output = self.done / "art.cleaned.webp"
        image = Image.new("RGBA", (10, 8), (25, 75, 125, 160))
        exif = Image.Exif()
        exif[0x010E] = "private EXIF"
        image.save(
            source,
            format="WEBP",
            lossless=True,
            exif=exif,
            xmp=b"<x:xmpmeta>private XMP</x:xmpmeta>",
            icc_profile=b"private ICC profile",
        )
        image.close()
        source_bytes = source.read_bytes()
        self.assertTrue(output_metadata._inspect_webp_metadata(source))

        result = clean_image_metadata_copy(source, output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(output_metadata._inspect_webp_metadata(output), ())
        self.assertEqual(set(result.removed_sections), {"EXIF", "XMP", "ICCP"})
        with Image.open(output) as cleaned:
            self.assertEqual(cleaned.size, (10, 8))
            self.assertIn("A", cleaned.getbands())
            cleaned.verify()

    def test_refuses_overwrite_and_same_source_destination(self) -> None:
        source = self.root / "mockup.png"
        output = self.done / "mockup.cleaned.png"
        create_rgba_png(source, with_metadata=False)
        output.parent.mkdir()
        output.write_bytes(b"keep")

        with self.assertRaises(FileExistsError):
            clean_image_metadata_copy(source, output)
        self.assertEqual(output.read_bytes(), b"keep")

        with self.assertRaises(OutputMetadataError):
            clean_image_metadata_copy(source, source)

    def test_validation_failure_leaves_no_partial_output(self) -> None:
        source = self.root / "mockup.png"
        output = self.done / "mockup.cleaned.png"
        create_rgba_png(source)
        source_bytes = source.read_bytes()

        with patch.object(
            output_metadata,
            "_verify_image_can_open",
            side_effect=OutputMetadataError("forced validation failure"),
        ):
            with self.assertRaises(OutputMetadataError):
                clean_image_metadata_copy(source, output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.done.glob(".*.metadata-*.tmp")))




if __name__ == "__main__":
    unittest.main()

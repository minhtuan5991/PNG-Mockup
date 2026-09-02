from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, PngImagePlugin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

import png_optimizer  # noqa: E402


def create_png(
    path: Path,
    *,
    color: tuple[int, int, int, int] = (20, 80, 160, 180),
    compress_level: int = 6,
) -> None:
    image = Image.new("RGBA", (96, 80), color)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("AutoMockupTest", "keep this metadata")
    image.save(
        path,
        format="PNG",
        compress_level=compress_level,
        dpi=(300, 300),
        icc_profile=b"automockup-print-profile",
        pnginfo=metadata,
    )
    image.close()


def create_pod_art(path: Path) -> None:
    size = 1400
    scale = 2
    image = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for y in range(180 * scale, 1220 * scale, 8):
        color = (20 + (y // scale) % 90, 65, 180, 242)
        draw.rectangle((180 * scale, y, 1220 * scale, y + 8), fill=color)
    draw.rounded_rectangle(
        (150 * scale, 150 * scale, 1250 * scale, 1250 * scale),
        radius=170 * scale,
        fill=(242, 75, 45, 238),
        outline=(255, 244, 220, 255),
        width=22 * scale,
    )
    draw.ellipse(
        (360 * scale, 320 * scale, 1040 * scale, 1000 * scale),
        fill=(20, 32, 45, 255),
        outline=(255, 210, 35, 255),
        width=26 * scale,
    )
    for number in range(14):
        x = (250 + number * 70) * scale
        draw.line(
            (x, 1040 * scale, x + 120 * scale, 1170 * scale),
            fill=(255, 255, 255, 245),
            width=9 * scale,
        )
    resized = image.resize((size, size), Image.Resampling.LANCZOS)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("AutoMockupTest", "safe to strip for POD output")
    resized.save(
        path,
        format="PNG",
        compress_level=6,
        dpi=(300, 300),
        icc_profile=b"automockup-print-profile",
        pnginfo=metadata,
    )
    resized.close()
    image.close()


class PngInspectionTests(unittest.TestCase):
    def test_compression_accepts_opaque_png_but_resize_still_requires_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opaque = root / "opaque.png"
            transparent = root / "transparent.png"
            empty = root / "empty.png"
            nearly_invisible = root / "alpha-1.png"
            create_png(opaque, color=(10, 20, 30, 255))
            create_png(transparent, color=(10, 20, 30, 128))
            create_png(empty, color=(10, 20, 30, 0))
            create_png(nearly_invisible, color=(10, 20, 30, 1))

            self.assertFalse(
                png_optimizer.inspect_png(opaque).can_resize_transparent
            )
            self.assertTrue(
                png_optimizer.inspect_png(transparent).can_resize_transparent
            )
            self.assertFalse(
                png_optimizer.inspect_png(empty).can_resize_transparent
            )
            self.assertFalse(
                png_optimizer.inspect_png(nearly_invisible).can_resize_transparent
            )

    def test_rejects_non_png_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake.png"
            fake.write_text("not a png", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Không đọc được PNG"):
                png_optimizer.inspect_png(fake)
            with self.assertRaises(FileNotFoundError):
                png_optimizer.inspect_png(root / "missing.png")


class PngOptimizationTests(unittest.TestCase):
    def test_bundled_binary_is_pinned_official_version_with_license(self) -> None:
        executable = png_optimizer.DEFAULT_OXIPNG_PATH
        license_path = executable.parent / "LICENSE"
        notice_path = executable.parent / "README-PNG-MOCKUP.txt"

        self.assertTrue(executable.is_file())
        self.assertTrue(license_path.is_file())
        self.assertTrue(notice_path.is_file())
        self.assertEqual(
            hashlib.sha256(executable.read_bytes()).hexdigest(),
            "394fef4ccbc6ee5a50ba96fe75af3557c4365349eb80371ef9ccc76f903c2530",
        )
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"oxipng {png_optimizer.OXIPNG_VERSION}", result.stdout)

    def test_bundled_libwebp_is_pinned_with_license_and_patent_grant(self) -> None:
        expected = {
            png_optimizer.DEFAULT_CWEBP_PATH: (
                "6a2f5cb5dce71366353ab1d9caf9c636e039f25703acfce1c148eed346f2f72a"
            ),
            png_optimizer.DEFAULT_DWEBP_PATH: (
                "17c1488bf84b7834e9aa908bb40afd0be2e55d57567d210e96336c56bb6ae993"
            ),
        }
        for executable, digest in expected.items():
            self.assertTrue(executable.is_file())
            self.assertEqual(hashlib.sha256(executable.read_bytes()).hexdigest(), digest)
            result = subprocess.run(
                [str(executable), "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn(png_optimizer.LIBWEBP_VERSION, result.stdout)
        self.assertTrue((png_optimizer.DEFAULT_CWEBP_PATH.parent / "COPYING").is_file())
        self.assertTrue((png_optimizer.DEFAULT_CWEBP_PATH.parent / "PATENTS").is_file())

    def test_real_pod_compression_hits_target_and_preserves_print_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Thiết kế POD.png"
            output = root / "Done Nén PNG" / source.name
            create_pod_art(source)
            source_bytes = source.read_bytes()

            result = png_optimizer.optimize_png_for_pod(source, output)

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(result.compression_mode.startswith("pod-near-lossless-"))
            self.assertTrue(result.target_achieved)
            self.assertGreaterEqual(result.saved_fraction, png_optimizer.POD_TARGET_SAVINGS)
            self.assertGreaterEqual(result.psnr_db, png_optimizer.POD_MIN_PSNR_DB)
            self.assertLessEqual(
                result.max_channel_delta,
                png_optimizer.POD_PRIMARY_MAX_CHANNEL_DELTA,
            )
            with Image.open(source) as original, Image.open(output) as optimized:
                original.load()
                optimized.load()
                self.assertEqual(original.size, optimized.size)
                self.assertEqual(
                    original.convert("RGBA").getchannel("A").tobytes(),
                    optimized.convert("RGBA").getchannel("A").tobytes(),
                )
                self.assertEqual(
                    original.info.get("icc_profile"), optimized.info.get("icc_profile")
                )
                self.assertEqual(original.info.get("dpi"), optimized.info.get("dpi"))
                self.assertNotIn("AutoMockupTest", optimized.info)

    def test_real_oxipng_reduces_size_without_changing_pixels_color_or_dpi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Ảnh in nguồn.png"
            output = root / "Done Nén PNG" / "Ảnh in nguồn.png"
            create_png(source, compress_level=0)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            result = png_optimizer.optimize_png_lossless(source, output)

            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            self.assertEqual(result.output_path, output.resolve())
            self.assertLess(result.output_bytes, result.input_bytes)
            self.assertGreater(result.saved_bytes, 0)
            with Image.open(source) as original, Image.open(output) as optimized:
                original.load()
                optimized.load()
                self.assertEqual(original.size, optimized.size)
                self.assertEqual(original.mode, optimized.mode)
                self.assertEqual(original.tobytes(), optimized.tobytes())
                self.assertEqual(
                    original.info.get("icc_profile"), optimized.info.get("icc_profile")
                )
                self.assertEqual(original.info.get("dpi"), optimized.info.get("dpi"))
                self.assertEqual(
                    original.info.get("AutoMockupTest"),
                    optimized.info.get("AutoMockupTest"),
                )

    def test_command_disables_transformations_and_uses_no_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source with spaces.png"
            output = root / "output with spaces.png"
            executable = root / "oxipng.exe"
            executable.write_bytes(b"test executable placeholder")
            create_png(source)
            captured: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                captured["command"] = command
                captured["kwargs"] = kwargs
                temporary_output = Path(command[command.index("--out") + 1])
                shutil.copy2(Path(command[-1]), temporary_output)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(png_optimizer.subprocess, "run", side_effect=fake_run):
                png_optimizer.optimize_png_lossless(
                    source,
                    output,
                    executable=executable,
                )

            command = captured["command"]
            self.assertIsInstance(command, list)
            self.assertIn("--nx", command)
            self.assertEqual(command[command.index("-o") + 1], "4")
            self.assertEqual(command[command.index("--threads") + 1], "2")
            self.assertNotIn("--alpha", command)
            self.assertNotIn("--strip", command)
            self.assertNotIn("--scale16", command)
            self.assertNotIn("--force", command)
            self.assertNotIn("shell", captured["kwargs"])

    def test_missing_or_failed_binary_never_publishes_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            output = root / "output.png"
            create_png(source)

            with self.assertRaisesRegex(FileNotFoundError, "Không tìm thấy oxipng"):
                png_optimizer.optimize_png_lossless(
                    source,
                    output,
                    executable=root / "missing.exe",
                )
            self.assertFalse(output.exists())

            executable = root / "oxipng.exe"
            executable.write_bytes(b"test executable placeholder")
            failed = subprocess.CompletedProcess([], 7, "", "simulated failure")
            with (
                patch.object(png_optimizer.subprocess, "run", return_value=failed),
                self.assertRaisesRegex(RuntimeError, "exit code 7"),
            ):
                png_optimizer.optimize_png_lossless(
                    source,
                    output,
                    executable=executable,
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("*.oxipng.tmp.png")), [])
            self.assertEqual(list(root.glob(".*.oxipng.tmp.png")), [])

    def test_existing_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            output = root / "output.png"
            create_png(source)
            output.write_bytes(b"existing user output")

            with self.assertRaises(FileExistsError):
                png_optimizer.optimize_png_lossless(source, output)
            self.assertEqual(output.read_bytes(), b"existing user output")

    def test_destination_created_during_optimization_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            output = root / "output.png"
            create_png(source)
            executable = root / "oxipng.exe"
            executable.write_bytes(b"test executable placeholder")
            real_fingerprint = png_optimizer._png_fingerprint

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                temporary_output = Path(command[command.index("--out") + 1])
                shutil.copy2(Path(command[-1]), temporary_output)
                return subprocess.CompletedProcess(command, 0, "", "")

            fingerprint_calls = 0

            def fingerprint_with_collision(path: Path) -> object:
                nonlocal fingerprint_calls
                result = real_fingerprint(path)
                fingerprint_calls += 1
                if fingerprint_calls == 2:
                    output.write_bytes(b"external user output")
                return result

            with (
                patch.object(png_optimizer.subprocess, "run", side_effect=fake_run),
                patch.object(
                    png_optimizer,
                    "_png_fingerprint",
                    side_effect=fingerprint_with_collision,
                ),
                self.assertRaises(FileExistsError),
            ):
                png_optimizer.optimize_png_lossless(
                    source,
                    output,
                    executable=executable,
                )

            self.assertEqual(output.read_bytes(), b"external user output")
            self.assertEqual(list(root.glob(".*.oxipng.tmp.png")), [])


class PngProcessingTests(unittest.TestCase):
    def test_compression_only_uses_original_as_optimizer_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            output = root / "Done Nén PNG" / "source.png"
            create_png(source)
            fake_result = png_optimizer.PngOptimizationResult(output, 1000, 600)

            with patch.object(
                png_optimizer,
                "optimize_png_for_pod",
                return_value=fake_result,
            ) as optimize:
                result = png_optimizer.process_png_file(
                    source,
                    output,
                    output_layout=None,
                    compress=True,
                    dpi=300,
                )

            optimize.assert_called_once_with(
                source.resolve(), output.resolve(), oxipng_executable=None
            )
            self.assertFalse(result.resized)
            self.assertTrue(result.compressed)
            self.assertEqual(result.saved_bytes, 400)

    def test_resize_only_keeps_existing_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            output = root / "Done 4200x4800" / "source.png"
            create_png(source)

            def fake_resize(
                source_path: Path,
                output_path: Path,
                output_layout: str,
                *,
                dpi: int,
            ) -> None:
                self.assertEqual(output_layout, "4200x4800")
                self.assertEqual(dpi, 300)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, output_path)

            with (
                patch(
                    "transparent_image_layout.resize_transparent_png",
                    side_effect=fake_resize,
                ) as resize,
                patch.object(png_optimizer, "optimize_png_for_pod") as optimize,
            ):
                result = png_optimizer.process_png_file(
                    source,
                    output,
                    output_layout="4200x4800",
                    compress=False,
                    dpi=300,
                )

            resize.assert_called_once()
            optimize.assert_not_called()
            self.assertTrue(result.resized)
            self.assertFalse(result.compressed)
            self.assertTrue(output.is_file())

    def test_combined_mode_resizes_then_compresses_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            output = root / "Done 4500x5400" / "source.png"
            create_png(source)
            calls: list[str] = []

            def fake_resize(
                source_path: Path,
                staging_path: Path,
                output_layout: str,
                *,
                dpi: int,
            ) -> None:
                calls.append("resize")
                self.assertEqual(output_layout, "4500x5400")
                staging_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, staging_path)

            def fake_optimize(
                staging_path: Path,
                output_path: Path,
                *,
                oxipng_executable: Path | None,
            ) -> png_optimizer.PngOptimizationResult:
                calls.append("compress")
                self.assertIsNone(oxipng_executable)
                self.assertTrue(staging_path.is_file())
                shutil.copy2(staging_path, output_path)
                return png_optimizer.PngOptimizationResult(output_path, 1000, 700)

            with (
                patch(
                    "transparent_image_layout.resize_transparent_png",
                    side_effect=fake_resize,
                ),
                patch.object(
                    png_optimizer,
                    "optimize_png_for_pod",
                    side_effect=fake_optimize,
                ),
            ):
                result = png_optimizer.process_png_file(
                    source,
                    output,
                    output_layout="4500x5400",
                    compress=True,
                    dpi=300,
                )

            self.assertEqual(calls, ["resize", "compress"])
            self.assertTrue(result.resized)
            self.assertTrue(result.compressed)
            self.assertEqual(result.saved_bytes, 300)
            self.assertTrue(output.is_file())
            self.assertEqual(list(output.parent.glob(".*.resize.tmp.png")), [])


if __name__ == "__main__":
    unittest.main()

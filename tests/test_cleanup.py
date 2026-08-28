from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from cleanup import CleanupReport, cleanup_app_files, _clear_temp_root, _remove_tree
from config import app_temp_root, load_config


def put(path: Path, text: str = "keep") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class PngCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        path = put(self.root / "app" / "config.json", "{}")
        self.config = load_config(path)
        self.config.ensure_folders()
        self.temp_patch = patch("config.tempfile.gettempdir", return_value=str(self.root / "temporary"))
        self.temp_patch.start()
        self.addCleanup(self.temp_patch.stop)

    def test_cleanup_preserves_images_config_queue_and_other_app_work(self) -> None:
        preserved = [
            put(self.config.png_dir / "design.png"),
            put(self.config.mockup_dir / "mockup.1S.psd"),
            put(self.config.output_dir / "final.png"),
            put(self.config.logs_dir / "queue.json", "[]"),
            put(self.config.logs_dir / "state.json", "{}"),
            put(self.config.logs_dir / "psd_done" / "done.json", "{}"),
            put(self.root / "temporary" / "GeminiMockupWF" / "psd_jobs" / "old.txt"),
            put(app_temp_root(self.root / "other-install") / "psd_jobs" / "other.txt"),
            self.config.config_path,
        ]
        removed = [
            put(self.config.logs_dir / "scratch.tmp"),
            put(self.config.temp_root / "psd_jobs" / "job" / "exports" / "temp.png"),
            put(self.config.project_root / "src" / "__pycache__" / "config.pyc"),
            put(self.config.project_root / "runtime" / "pip-cache" / "download.bin"),
        ]
        report = cleanup_app_files(self.config)
        self.assertFalse(report.warnings, report.warnings)
        self.assertEqual(report.removed_files, len(removed))
        self.assertTrue(all(path.exists() for path in preserved))
        self.assertFalse(any(path.exists() for path in removed))

    def test_only_this_install_temp_root_is_accepted(self) -> None:
        other = app_temp_root(self.root / "other-install")
        protected = put(other / "keep.bin")
        report = CleanupReport()
        _clear_temp_root(other, self.config.project_root, report)
        _clear_temp_root(other.parent, self.config.project_root, report)
        self.assertTrue(protected.exists())
        self.assertEqual(len(report.warnings), 2)
        self.assertNotEqual(self.config.temp_root, other)
        self.assertEqual(self.config.temp_root, app_temp_root(self.config.project_root))

    def test_recursive_delete_rejects_root_and_external_path(self) -> None:
        outside = self.root / "outside"
        protected = put(outside / "keep.bin")
        report = CleanupReport()
        _remove_tree(self.config.project_root, self.config.project_root, report)
        _remove_tree(outside, self.config.project_root, report)
        self.assertTrue(protected.exists())
        self.assertTrue(self.config.config_path.exists())
        self.assertEqual(len(report.warnings), 2)

    def test_log_is_trimmed_to_last_300_lines(self) -> None:
        lines = [f"line {index}" for index in range(500)]
        put(self.config.log_path, "\n".join(lines) + "\n")
        report = cleanup_app_files(self.config)
        self.assertFalse(report.warnings)
        self.assertEqual(self.config.log_path.read_text(encoding="utf-8").splitlines(), lines[-300:])

    def test_runtime_pip_cache_can_be_preserved(self) -> None:
        preserved = put(self.config.project_root / "runtime" / "pip-cache" / "keep.bin")
        cleanup_app_files(self.config, include_pip_cache=False)
        self.assertTrue(preserved.exists())


if __name__ == "__main__":
    unittest.main()

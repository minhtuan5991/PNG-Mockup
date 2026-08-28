from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

import update_manager


class ProductIdentityTests(unittest.TestCase):
    def test_version_requires_matching_product_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version_file = root / "version.json"
            version_file.write_text(json.dumps({"product_id": "another-app", "version": "99.0.0"}), encoding="utf-8")
            self.assertEqual(update_manager.read_current_version(root), "0.0.0")
            version_file.write_text(json.dumps({"product_id": "png-mockup", "version": "1.2.3"}), encoding="utf-8")
            self.assertEqual(update_manager.read_current_version(root), "1.2.3")

    def test_old_repository_cannot_enable_network_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("socket.socket", side_effect=AssertionError("No network permitted")):
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({
                "update_github_repo": "minhtuan5991/AutoDesignPOD",
                "update_check_on_start": True,
            }), encoding="utf-8")
            self.assertIsNone(update_manager.check_latest_release(root))
            self.assertFalse(update_manager.auto_check_due(root, 1))
            with self.assertRaises(update_manager.UpdateError):
                update_manager.normalize_github_repo("minhtuan5991/AutoDesignPOD")

    def test_supplied_release_cannot_download_or_launch(self) -> None:
        release = update_manager.ReleaseInfo("99.0.0", "v99.0.0", "", "", "https://example.invalid/app.zip", 1, "0" * 64)
        with tempfile.TemporaryDirectory() as directory, patch("socket.socket", side_effect=AssertionError("No network permitted")):
            root = Path(directory)
            self.assertIsNone(update_manager.download_release(release, root))
            with self.assertRaises(update_manager.UpdateError):
                update_manager.launch_updater(root / "untrusted.exe", root, wait_pid=12345)


if __name__ == "__main__":
    unittest.main()

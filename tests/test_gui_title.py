from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from gui import app_window_title  # noqa: E402


class WindowTitleTests(unittest.TestCase):
    def test_title_uses_the_installed_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app_root = Path(temporary)
            (app_root / "version.json").write_text(
                json.dumps({"product_id": "png-mockup", "version": "1.2.9"}),
                encoding="utf-8",
            )

            self.assertEqual(app_window_title(app_root), "PNG Mockup v1.2.9")

    def test_title_does_not_duplicate_an_existing_v_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app_root = Path(temporary)
            (app_root / "version.json").write_text(
                json.dumps({"product_id": "png-mockup", "version": "v2.0.0"}),
                encoding="utf-8",
            )

            self.assertEqual(app_window_title(app_root), "PNG Mockup v2.0.0")


    def test_title_does_not_adopt_another_products_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app_root = Path(temporary)
            (app_root / "version.json").write_text(
                json.dumps({"product_id": "another-product", "version": "9.9.9"}),
                encoding="utf-8",
            )
            self.assertEqual(app_window_title(app_root), "PNG Mockup v0.0.0")


if __name__ == "__main__":
    unittest.main()

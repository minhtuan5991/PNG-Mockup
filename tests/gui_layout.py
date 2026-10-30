"""Map the real workspace and check controls at narrow widths / high DPI."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "App Source" / "src"))

import config
from gui import PngMockupGui
from image_tools import inspect_tool_image
from ui_themes import COLOR_PRESETS


def main() -> int:
    cases = []
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        config.PROJECT_ROOT = directory
        sample = directory / "Ảnh kiểm tra.png"
        with Image.new("RGBA", (160, 180), (30, 120, 190, 170)) as image:
            image.save(sample)
        for scale in (1.0, 1.5, 2.0):
            (directory / "config.json").write_text(json.dumps({"ui_color_preset": "mint"}), encoding="utf-8")
            root = tk.Tk()
            root.tk.call("tk", "scaling", (96 / 72) * scale)
            with patch.object(PngMockupGui, "_maybe_auto_check_updates"):
                app = PngMockupGui(root)
            app.show_page("tools")
            workspace = app.image_tools
            workspace.images = {sample: inspect_tool_image(sample)}
            workspace.selected = {sample}
            workspace._selection_changed()
            for geometry in ("1060x660+40+40", "1320x880+40+40"):
                root.geometry(geometry)
                for preset in COLOR_PRESETS:
                    app.apply_theme("png_modern", preset)
                    for expanded in (False, True):
                        for variable in workspace.features.values():
                            variable.set(expanded)
                        workspace._options_changed()
                        root.update()
                        root.update_idletasks()
                        for button in (workspace.start_button, workspace.open_button, app.buttons["image_tools_choose"], *workspace.feature_checks.values()):
                            assert button.winfo_ismapped(), (scale, geometry, preset, button)
                            assert button.winfo_width() >= button.winfo_reqwidth(), (scale, geometry, preset, button.cget("text"), "width", button.winfo_width(), button.winfo_reqwidth())
                            assert button.winfo_height() >= button.winfo_reqheight(), (scale, geometry, preset, button.cget("text"), "height")
                            assert button.winfo_rootx() >= root.winfo_rootx(), (button, "left")
                            assert button.winfo_rootx() + button.winfo_width() <= root.winfo_rootx() + root.winfo_width(), (scale, geometry, preset, button.cget("text"), "right")
                        for button in (workspace.start_button, workspace.open_button):
                            assert button.winfo_rooty() + button.winfo_height() <= root.winfo_rooty() + root.winfo_height(), (button, "bottom")
                        cases.append((scale, geometry, preset, expanded))
            # Gallery selection has a keyboard alternative to mouse/dragging.
            workspace._keyboard_move(SimpleNamespace(keysym="Home"))
            before = bool(workspace.selected)
            workspace._keyboard_toggle(None)
            assert bool(workspace.selected) != before
            app._stop_update_worker()
            root.destroy()
            if workspace._thumb_thread:
                workspace._thumb_thread.join(timeout=2)
    print(f"GUI layout passed: {len(cases)} mapped configurations, five palettes, 100/150/200% scaling, inline options, keyboard gallery and fixed action footer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

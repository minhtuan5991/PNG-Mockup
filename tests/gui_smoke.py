from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk
from unittest.mock import patch

from PIL import Image, ImageTk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

import config  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root_path = Path(temporary)
        (root_path / "config.json").write_text(
            json.dumps(
                {
                    "ui_theme": "png_modern",
                    "ui_color_preset": "soft_purple",
                    "update_check_on_start": False,
                    "update_github_repo": "legacy/ignored",
                    "update_check_interval_hours": 720,
                }
            ),
            encoding="utf-8",
        )
        config.PROJECT_ROOT = root_path

        from gui import PngMockupGui

        window = tk.Tk()
        window.withdraw()
        # Every test window stays withdrawn; this is not screenshot automation.
        class HiddenToplevel(tk.Toplevel):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.withdraw()

        hidden_dialogs = patch("gui.tk.Toplevel", HiddenToplevel)
        hidden_dialogs.start()
        disabled_channel = patch(
            "gui.update_manager.read_channel",
            return_value=SimpleNamespace(enabled=False, repository="", reason="Chưa cấu hình GitHub riêng."),
            create=True,
        )
        disabled_channel.start()
        app = PngMockupGui(window)
        window.update_idletasks()

        installed_version = json.loads(
            (SOURCE_ROOT / "version.json").read_text(encoding="utf-8")
        )["version"]
        version_label = (
            installed_version
            if str(installed_version).lower().startswith("v")
            else f"v{installed_version}"
        )
        assert window.title() == f"PNG Mockup {version_label}"
        assert app.theme_key == "png_modern"

        # The tools workspace accepts files directly and uses inline options.
        tools = app.image_tools
        assert {key for key in app.buttons if key.startswith("rename_")} == {
            "rename_png", "rename_mockup",
        }
        assert set(tools.features) == {"resize", "compress", "metadata", "watermark"}
        assert not any(variable.get() for variable in tools.features.values())
        assert tools.start_button.instate(["disabled"])
        assert not tools.option_panels["compress"].winfo_manager()

        def pump_until(predicate, timeout=15):
            deadline = time.monotonic() + timeout
            while not predicate():
                window.update()
                if time.monotonic() > deadline:
                    raise AssertionError("Image tools timed out")
                time.sleep(0.01)
            window.update()

        from PIL.PngImagePlugin import PngInfo
        image_source = root_path / "Ảnh đầu vào.png"
        metadata = PngInfo()
        metadata.add_text("Comment", "remove this")
        Image.new("RGB", (96, 80), (30, 120, 210)).save(
            image_source, compress_level=0, dpi=(300, 300),
            icc_profile=b"gui-smoke-print-profile", pnginfo=metadata,
        )
        original_bytes = image_source.read_bytes()
        with (
            patch("image_tools_ui.filedialog.askopenfilenames", return_value=(str(image_source),)) as choose,
            patch("image_tools_ui.filedialog.askdirectory") as folder_prompt,
        ):
            app.buttons["image_tools_choose"].invoke()
        choose.assert_called_once()
        folder_prompt.assert_not_called()
        pump_until(lambda: not tools.loading)
        assert len(tools.images) == 1 and image_source.resolve() in tools.selected

        # Exercise the real Unicode Explorer drop handler, not just add_files().
        import ctypes
        import os
        if os.name == "nt":
            assert tools.drop_target.available
            dropped_source = root_path / "Tệp kéo thả.png"
            Image.new("RGBA", (48, 40), (20, 80, 160, 180)).save(dropped_source)
            from ctypes import wintypes
            class DropFiles(ctypes.Structure):
                _fields_ = [("pFiles", wintypes.DWORD), ("pt", wintypes.POINT), ("fNC", wintypes.BOOL), ("fWide", wintypes.BOOL)]
            names = (str(dropped_source) + "\0" + str(image_source) + "\0\0").encode("utf-16-le")
            header = DropFiles(ctypes.sizeof(DropFiles), wintypes.POINT(0, 0), False, True)
            payload = bytes(header) + names
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel.GlobalLock.restype = ctypes.c_void_p
            kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            handle = kernel.GlobalAlloc(0x42, len(payload))
            assert handle
            pointer = kernel.GlobalLock(handle)
            ctypes.memmove(pointer, payload, len(payload))
            kernel.GlobalUnlock(handle)
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
            user.SendMessageW.restype = ctypes.c_ssize_t
            user.SendMessageW(tools.drop_target._hwnd, 0x0233, handle, 0)
            pump_until(lambda: dropped_source.resolve() in tools.images and not tools.loading)
            assert len(tools.images) == 2  # duplicate is ignored
            tools.selected = {dropped_source.resolve()}
            tools.remove_selected()
            assert len(tools.images) == 1
            tools.select_all(True)

        tools.feature_checks["compress"].invoke()
        assert tools.option_panels["compress"].winfo_manager() == "pack"
        assert tools.start_button.instate(["!disabled"])
        with patch("image_tools_ui.threading.Thread.start", side_effect=RuntimeError("test launch failure")):
            tools.start_button.invoke()
        assert not app.maintenance_running
        assert tools.start_button.instate(["!disabled"])
        tools.start_button.invoke()
        pump_until(lambda: not app.maintenance_running)
        compression_dir = tools.output_dir
        assert compression_dir and compression_dir.is_dir()
        compressed = compression_dir / image_source.name
        assert compressed.is_file() and compressed.stat().st_size < len(original_bytes)
        assert image_source.read_bytes() == original_bytes
        with Image.open(compressed) as optimized:
            assert optimized.size == (96, 80)
            assert optimized.info["icc_profile"] == b"gui-smoke-print-profile"
            assert abs(optimized.info["dpi"][0] - 300) < 0.02
        tools.feature_checks["compress"].invoke()
        tools.feature_checks["metadata"].invoke()
        tools.start_button.invoke()
        pump_until(lambda: not app.maintenance_running)
        assert tools.output_dir != compression_dir
        with Image.open(tools.output_dir / image_source.name) as cleaned:
            assert "Comment" not in cleaned.info
            assert "icc_profile" not in cleaned.info
            assert cleaned.size == (96, 80)
        assert image_source.read_bytes() == original_bytes
        tools.feature_checks["watermark"].invoke()
        assert tools.features["metadata"].get()
        assert tools.feature_checks["metadata"].instate(["disabled"])
        assert tools.option_panels["watermark"].winfo_manager() == "pack"
        tools.feature_checks["watermark"].invoke()
        assert tools.feature_checks["metadata"].instate(["!disabled"])

        def all_widgets(parent):
            pending = [parent]
            while pending:
                widget = pending.pop()
                yield widget
                pending.extend(widget.winfo_children())

        def control(parent, kind, text):
            return next(widget for widget in all_widgets(parent)
                        if isinstance(widget, kind) and str(widget.cget("text")) == text)

        # Exercise the two Tạo mockup buttons through their real dialogs.
        app.show_page("mockup")
        folders = config.load_config()
        png_source = folders.png_dir / "Thiết kế.wh.b (2).png"
        with Image.new("RGBA", (32, 32), (30, 120, 210, 255)) as image:
            image.save(png_source)
        psb_source = folders.mockup_dir / "Mẫu áo.bl.1S.PSB"
        psb_source.write_bytes(b"template placeholder; preview must not decode PSB")
        rename_cases = (
            ("png", "PNG", png_source, "Thiết kế.bl.f.png",
             ("Áo sáng màu (.bl)", "Mặt trước (.f)")),
            ("mockup", "Mockup", psb_source, "Mẫu áo.2S.PSB",
             ("Áo tối màu (.wh)", "Mockup 2 Mặt (.2S)")),
        )
        for key, label, source, new_name, markers in rename_cases:
            button = app.buttons[f"rename_{key}"]
            assert button.master is app.buttons[key].master
            assert str(button.cget("text")) == "Đổi Tên"
            assert str(button.cget("style")) == "Bold.TButton"
            original = source.read_bytes()
            errors: list[Exception] = []

            def rename_in_dialog():
                dialog = None
                try:
                    dialog = next(
                        child for child in window.winfo_children()
                        if isinstance(child, tk.Toplevel)
                        and child.title() == f"Đổi tên file - {label}"
                    )
                    control(dialog, ttk.Button, "Chọn tất cả").invoke()
                    for marker in markers:
                        control(dialog, ttk.Checkbutton, marker).invoke()
                    preview = f"{source.name}  →  {new_name}"
                    label_variables = [
                        str(widget.cget("textvariable"))
                        for widget in all_widgets(dialog) if isinstance(widget, ttk.Label)
                    ]
                    assert any(dialog.getvar(name) == preview for name in label_variables if name)
                    rename_button = control(dialog, ttk.Button, "Đổi tên")
                    close_button = control(dialog, ttk.Button, "Đóng")
                    assert rename_button.master is close_button.master
                    assert rename_button.master.pack_info()["side"] == "bottom"
                    rename_button.invoke()
                    close_button.invoke()
                except Exception as exc:
                    errors.append(exc)
                    if dialog is not None and dialog.winfo_exists():
                        dialog.destroy()

            with patch("gui.messagebox.showerror", side_effect=AssertionError("Rename failed")):
                after_id = window.after(25, rename_in_dialog)
                button.invoke()
                window.after_cancel(after_id)
            assert not errors, errors
            assert source.with_name(new_name).read_bytes() == original
            assert not source.exists()

        open_path_target = root_path / "folder-open-speed-test"
        open_path_called = threading.Event()

        def record_open_path(raw_path: str) -> None:
            assert Path(raw_path) == open_path_target
            open_path_called.set()

        with patch("gui.os.startfile", side_effect=record_open_path):
            app.open_path(open_path_target)
            assert open_path_called.wait(2), "Folder opening worker did not start promptly"
        assert open_path_target.is_dir()

        assert set(app.nav_buttons) == {"mockup", "tools", "settings"}
        assert not hasattr(app, "source_vars")
        for removed in ("open_login_edge", "validate_background_pipeline", "open_prompt_editor"):
            assert not hasattr(app, removed)
        for widget in all_widgets(window):
            if "text" in widget.keys():
                text = str(widget.cget("text")).casefold()
                assert not any(term in text for term in ("idea", "gemini", "xóa nền", "printarea", "prompt")), text

        # The same widgets restyle immediately; no remote assets are required.
        from ui_themes import COLOR_PRESETS
        icon_colors = set()
        for preset in COLOR_PRESETS:
            app.apply_theme("png_modern", preset)
            window.update_idletasks()
            assert app.theme.color_preset == preset
            assert str(window.cget("background")) == app.theme.bg
            assert str(app.output.cget("foreground")) == app.theme.log_fg
            assert ttk.Style(window).lookup("Primary.TButton", "background") == app.theme.accent
            assert ttk.Style(window).lookup("SoftGreenOutline.TButton", "bordercolor") == app.theme.soft_green_border
            assert "bold" in str(ttk.Style(window).lookup("Bold.TButton", "font")).casefold()
            assert "bold" in str(ttk.Style(window).lookup("BoldDanger.TButton", "font")).casefold()
            assert "bold" in str(ttk.Style(window).lookup("BoldQuiet.TButton", "font")).casefold()
            icon_colors.add(ImageTk.getimage(app.button_icon_refs[str(app.buttons["run"])]).tobytes())
            for page in ("mockup", "tools", "settings"):
                app.show_page(page)
                assert app.current_page == page
                assert str(app.nav_buttons[page].cget("style")) == "Selected.Nav.TButton"
        # Light presets use white on primary; dark uses a dark foreground.
        assert len(icon_colors) >= 2

        app.open_settings()
        assert app.current_page == "settings"
        assert set(app.settings_entries) == {
            "generation_timeout_sec", "output_width_px", "output_height_px", "output_dpi"
        }
        assert app.settings_checks["remove_output_metadata"].get()
        assert app.settings_checks["completion_notification_enabled"].get()
        assert not app.settings_checks["completion_sound_enabled"].get()
        app.palette_var.set("dark")
        with patch("gui.messagebox.showerror") as settings_error:
            app.buttons["settings"].invoke()
        settings_error.assert_not_called()
        saved = json.loads((root_path / "config.json").read_text(encoding="utf-8"))
        assert saved["ui_color_preset"] == "dark"
        assert saved["ui_theme"] == "png_modern"
        assert saved["update_github_repo"] == "legacy/ignored"
        assert saved["update_check_on_start"] is False
        assert saved["update_check_interval_hours"] == 720
        # The actual Settings save retains a hidden/read-only config and prefs.
        temporary_config = root_path / "config.json"
        if hasattr(temporary_config.stat(), "st_file_attributes"):
            app.set_windows_file_attributes(temporary_config, "+h", "+r")
            assert temporary_config.stat().st_file_attributes & 0x3 == 0x3
            with patch("gui.messagebox.showerror") as hidden_settings_error:
                app.save_settings()
            hidden_settings_error.assert_not_called()
            assert temporary_config.stat().st_file_attributes & 0x3 == 0x3
            assert json.loads(temporary_config.read_text(encoding="utf-8")) == saved
            app.set_windows_file_attributes(temporary_config, "-h", "-r")
        app.apply_theme("png_modern", "mint")
        app._restore_settings()
        assert app.color_preset_key == "dark"
        before_invalid_save = (root_path / "config.json").read_bytes()
        timeout = app.settings_entries["generation_timeout_sec"]
        timeout.delete(0, "end")
        timeout.insert(0, "0")
        with patch("gui.messagebox.showerror") as invalid_settings:
            app.save_settings()
        invalid_settings.assert_called_once()
        assert (root_path / "config.json").read_bytes() == before_invalid_save
        app._restore_settings()

        app.captured_task_running = True
        app.update_buttons()
        assert all(app.buttons[key].instate(["disabled"]) for key in app.locked_buttons)
        app.captured_task_running = False
        app.update_buttons()
        assert not app.buttons["run"].instate(["disabled"])
        assert app.buttons["stop"].instate(["disabled"])
        assert app.buttons["force_stop"].instate(["disabled"])
        assert not app.output_watermark_enabled_var.get()
        assert app.output_watermark_choose_button.instate(["disabled"])
        with patch("gui.filedialog.askopenfilename", return_value=""):
            app.output_watermark_enabled_var.set(True)
            app.on_output_watermark_toggle()
        assert not app.output_watermark_enabled_var.get()
        watermark = root_path / "studio-watermark.png"
        Image.new("RGBA", (2000, 2000), (240, 30, 80, 128)).save(
            watermark, dpi=(72, 72)
        )
        with patch("gui.filedialog.askopenfilename", return_value=str(watermark)):
            assert app.choose_output_watermark()
        assert app.output_watermark_enabled_var.get()
        assert app.output_watermark_path_display_var.get() == watermark.name
        assert config.load_config().output_watermark_path == watermark.resolve()
        assert app.validate_output_watermark()

        from task_completion import RunSummary
        with patch("gui.play_system_notification") as play_sound:
            app._notify_task_completion("Tạo mockup PNG", 0, 65,
                                        RunSummary(completed=2, failed=0), forced=False)
            window.update_idletasks()
        play_sound.assert_not_called()
        assert app.completion_popup is not None
        assert app.completion_popup.state() == "withdrawn"
        assert window.grab_current() is None
        app._close_completion_popup()
        assert app.completion_popup is None

        app._apply_job_progress([
            {"status": "done"}, {"status": "failed"},
            {"status": "running"}, {"status": "pending"},
        ])
        assert app.count_vars["total"].get() == "4"
        assert app.count_vars["pending"].get() == "2"
        assert app.count_vars["done"].get() == "1"
        assert app.count_vars["failed"].get() == "1"
        assert app.progress_var.get() == 50.0
        # No channel is shown quietly; the false legacy startup flag was ignored.
        pump_until(lambda: app.update_status_var.get() == "Chưa cấu hình GitHub riêng.")
        assert app.update_popup is None
        assert app.update_status_var.get() == "Chưa cấu hình GitHub riêng."
        assert app.buttons["install_update"].instate(["disabled"])
        app._show_update_notice("1.2.0")
        assert app.update_popup is not None
        assert app.update_popup.state() == "withdrawn"
        assert window.grab_current() is None
        app._close_update_notice()
        assert app.update_popup is None
        assert window.state() == "withdrawn"
        app._stop_update_worker()
        if app._update_thread is not None:
            app._update_thread.join(timeout=2)
            assert not app._update_thread.is_alive()
        window.destroy()
        hidden_dialogs.stop()
        disabled_channel.stop()
    print("GUI smoke checks passed: five palettes, three pages, hidden config/settings, file drop, inline tools, compression, metadata, task controls, updates, notifications; all windows withdrawn")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

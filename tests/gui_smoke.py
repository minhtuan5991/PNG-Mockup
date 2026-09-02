from __future__ import annotations

import json
import sys
import tempfile
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

        thumbnail_source = root_path / "rename-thumbnail.png"
        Image.new("RGBA", (640, 640), (30, 120, 210, 255)).save(thumbnail_source)
        first_thumbnail = app._rename_thumbnail(thumbnail_source)
        assert app._rename_thumbnail(thumbnail_source) is first_thumbnail
        renamed_thumbnail_source = root_path / "rename-thumbnail.bl.f.png"
        thumbnail_source.rename(renamed_thumbnail_source)
        app._transfer_rename_thumbnail_cache(
            thumbnail_source, renamed_thumbnail_source
        )
        assert app._rename_thumbnail(renamed_thumbnail_source) is first_thumbnail

        psd_source = root_path / "large-mockup.psd"
        psd_source.write_bytes(b"placeholder only")
        with patch("gui.Image.open", side_effect=AssertionError("PSD was decoded")):
            psd_thumbnail = app._rename_thumbnail(psd_source)
        assert psd_thumbnail is app._rename_thumbnail(psd_source)

        button_keys = list(app.buttons)
        assert "batch_metadata_clean" in button_keys
        assert (
            button_keys.index("batch_metadata_clean")
            == button_keys.index("batch_resize") + 1
        )
        assert (
            str(app.buttons["batch_metadata_clean"].cget("text"))
            == "Chọn ảnh để xử lý"
        )
        assert (
            str(app.buttons["batch_resize"].cget("text"))
            == "Nén ảnh PNG"
        )
        assert str(app.buttons["png"].cget("style")) == "Bold.TButton"
        assert str(app.buttons["mockup"].cget("style")) == "Bold.TButton"
        assert str(app.buttons["rename_png"].cget("text")) == "Đổi Tên"
        assert str(app.buttons["rename_mockup"].cget("text")) == "Đổi Tên"
        assert str(app.buttons["rename_png"].cget("style")) == "Bold.TButton"
        assert str(app.buttons["rename_mockup"].cget("style")) == "Bold.TButton"
        assert str(app.buttons["force_stop"].cget("style")) == "BoldDanger.TButton"
        assert str(app.buttons["output"].cget("style")) == "BoldQuiet.TButton"
        assert str(app.output_watermark_choose_button.cget("style")) == "SoftGreenOutline.TButton"
        for key in ("refresh_top", "reset_top", "run_error_top"):
            assert str(app.buttons[key].cget("style")) == "SoftGreenOutline.TButton"

        from png_optimizer import (
            POD_PRIMARY_MAX_CHANNEL_DELTA,
            PngImageInfo,
            _measure_composited_quality,
            inspect_png,
        )

        resize_source = root_path / "resize-dialog-source.png"
        Image.new("RGBA", (96, 80), (30, 120, 210, 180)).save(
            resize_source
        )
        resize_info = PngImageInfo(
            path=resize_source,
            width=4200,
            height=4800,
            mode="RGBA",
            alpha_extrema=(180, 180),
        )
        resize_dialog_errors: list[str] = []

        def inspect_and_close_resize_dialog() -> None:
            try:
                resize_dialog = next(
                    child
                    for child in window.winfo_children()
                    if isinstance(child, tk.Toplevel)
                    and child.title() == "Thay đổi kích thước/Nén ảnh PNG"
                )
                pending_resize_widgets = [resize_dialog]
                resize_texts: set[str] = set()
                resize_checks: dict[str, ttk.Checkbutton] = {}
                close_resize_button: ttk.Button | None = None
                start_resize_button: ttk.Button | None = None
                while pending_resize_widgets:
                    widget = pending_resize_widgets.pop()
                    pending_resize_widgets.extend(widget.winfo_children())
                    try:
                        text = str(widget.cget("text"))
                    except tk.TclError:
                        continue
                    if text:
                        resize_texts.add(text)
                    if isinstance(widget, ttk.Checkbutton) and text in {
                        "4500x5400",
                        "4200x4800",
                        "Nén ảnh PNG",
                    }:
                        resize_checks[text] = widget
                    if isinstance(widget, ttk.Button) and text == "Đóng":
                        close_resize_button = widget
                    if isinstance(widget, ttk.Button) and text == "Bắt đầu xử lý":
                        start_resize_button = widget

                assert set(resize_checks) == {
                    "4500x5400",
                    "4200x4800",
                    "Nén ảnh PNG",
                }
                assert any(
                    "một chế độ nén tự động" in text
                    and "alpha, ICC và DPI" in text
                    for text in resize_texts
                )

                def checked(name: str) -> bool:
                    check = resize_checks[name]
                    return bool(
                        resize_dialog.tk.getboolean(
                            resize_dialog.getvar(str(check.cget("variable")))
                        )
                    )

                assert checked("Nén ảnh PNG")
                assert not checked("4500x5400")
                assert not checked("4200x4800")

                resize_checks["4500x5400"].invoke()
                assert checked("4500x5400")
                assert not checked("4200x4800")
                assert checked("Nén ảnh PNG")

                resize_checks["4200x4800"].invoke()
                assert not checked("4500x5400")
                assert checked("4200x4800")
                assert checked("Nén ảnh PNG"), (
                    "Compression must remain selected when the resize size changes"
                )
                assert close_resize_button is not None
                assert start_resize_button is not None
                with (
                    patch(
                        "gui.threading.Thread.start",
                        side_effect=RuntimeError("simulated thread start failure"),
                    ),
                    patch("gui.messagebox.showerror") as showerror,
                ):
                    start_resize_button.invoke()
                assert not app.maintenance_running
                assert str(start_resize_button.cget("state")) == "normal"
                assert str(close_resize_button.cget("state")) == "normal"
                showerror.assert_called_once()
                close_resize_button.invoke()
            except Exception as exc:
                resize_dialog_errors.append(str(exc))
                for child in window.winfo_children():
                    if isinstance(child, tk.Toplevel) and child.title() == (
                        "Thay đổi kích thước/Nén ảnh PNG"
                    ):
                        child.destroy()

        window.after(50, inspect_and_close_resize_dialog)
        app.open_batch_resize_options(root_path, [resize_info])
        assert not resize_dialog_errors, resize_dialog_errors
        assert not any(
            isinstance(child, tk.Toplevel)
            and child.title() == "Thay đổi kích thước/Nén ảnh PNG"
            for child in window.winfo_children()
        ), "Resize/compression dialog must close cleanly without hanging"

        compression_source = root_path / "compression-ui-source.png"
        compression_image = Image.new("RGB", (96, 80), (30, 120, 210))
        compression_image.save(
            compression_source,
            format="PNG",
            compress_level=0,
            dpi=(300, 300),
            icc_profile=b"gui-smoke-print-profile",
        )
        compression_image.close()
        compression_source_bytes = compression_source.read_bytes()
        compression_info = inspect_png(compression_source)
        assert not compression_info.can_resize_transparent
        compression_dialog_errors: list[str] = []
        compression_deadline = time.monotonic() + 10

        def start_compression_only() -> None:
            try:
                compression_dialog = next(
                    child
                    for child in window.winfo_children()
                    if isinstance(child, tk.Toplevel)
                    and child.title() == "Thay đổi kích thước/Nén ảnh PNG"
                )
                pending_widgets = [compression_dialog]
                compression_check: ttk.Checkbutton | None = None
                start_button: ttk.Button | None = None
                close_button: ttk.Button | None = None
                while pending_widgets:
                    widget = pending_widgets.pop()
                    pending_widgets.extend(widget.winfo_children())
                    try:
                        text = str(widget.cget("text"))
                    except tk.TclError:
                        continue
                    if isinstance(widget, ttk.Checkbutton) and text == "Nén ảnh PNG":
                        compression_check = widget
                    elif isinstance(widget, ttk.Button) and text == "Bắt đầu xử lý":
                        start_button = widget
                    elif isinstance(widget, ttk.Button) and text == "Đóng":
                        close_button = widget
                assert compression_check is not None
                assert start_button is not None
                assert close_button is not None
                assert compression_dialog.tk.getboolean(
                    compression_dialog.getvar(str(compression_check.cget("variable")))
                )
                start_button.invoke()

                def close_when_finished() -> None:
                    try:
                        output = root_path / "Done Nén PNG" / compression_source.name
                        if output.is_file() and str(close_button.cget("state")) == "normal":
                            close_button.invoke()
                            return
                        if time.monotonic() >= compression_deadline:
                            raise AssertionError("Compression-only UI run timed out")
                        compression_dialog.after(50, close_when_finished)
                    except Exception as exc:
                        compression_dialog_errors.append(str(exc))
                        compression_dialog.destroy()

                compression_dialog.after(50, close_when_finished)
            except Exception as exc:
                compression_dialog_errors.append(str(exc))
                for child in window.winfo_children():
                    if isinstance(child, tk.Toplevel) and child.title() == (
                        "Thay đổi kích thước/Nén ảnh PNG"
                    ):
                        child.destroy()

        with (
            patch("gui.messagebox.showinfo") as compression_notice,
            patch("gui.messagebox.showwarning") as compression_warning,
            patch("gui.messagebox.showerror") as compression_error,
        ):
            window.after(50, start_compression_only)
            app.open_batch_resize_options(root_path, [compression_info])
        assert not compression_dialog_errors, compression_dialog_errors
        assert compression_notice.called
        assert not compression_warning.called
        assert not compression_error.called
        compression_output = root_path / "Done Nén PNG" / compression_source.name
        assert compression_output.is_file()
        assert compression_source.read_bytes() == compression_source_bytes
        assert compression_output.stat().st_size < compression_source.stat().st_size
        with Image.open(compression_source) as original, Image.open(
            compression_output
        ) as optimized:
            original.load()
            optimized.load()
            assert original.size == optimized.size
            original_rgba = original.convert("RGBA")
            optimized_rgba = optimized.convert("RGBA")
            try:
                assert _measure_composited_quality(
                    original_rgba, optimized_rgba
                ).max_channel_delta <= POD_PRIMARY_MAX_CHANNEL_DELTA
            finally:
                original_rgba.close()
                optimized_rgba.close()
            assert original.info.get("icc_profile") == optimized.info.get("icc_profile")
            assert original.info.get("dpi") == optimized.info.get("dpi")

        def all_widgets(parent):
            pending = [parent]
            while pending:
                widget = pending.pop()
                yield widget
                pending.extend(widget.winfo_children())

        def control(parent, kind, text):
            return next(widget for widget in all_widgets(parent)
                        if isinstance(widget, kind) and str(widget.cget("text")) == text)

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

        # Rename uses the real file operations and retains old naming rules.
        rename_folder = config.load_config().png_dir
        rename_source = rename_folder / "shirt.png"
        Image.new("RGBA", (64, 64), (80, 100, 180, 200)).save(rename_source)
        rename_errors = []
        def rename_dialog_action():
            dialog = next(child for child in window.winfo_children() if isinstance(child, HiddenToplevel))
            try:
                assert dialog.state() == "withdrawn"
                control(dialog, ttk.Button, "Chọn tất cả").invoke()
                control(dialog, ttk.Checkbutton, "Áo sáng màu (.bl)").invoke()
                control(dialog, ttk.Checkbutton, "Mặt trước (.f)").invoke()
                control(dialog, ttk.Button, "Đổi tên").invoke()
                assert (rename_folder / "shirt.bl.f.png").is_file()
                assert not rename_source.exists()
                control(dialog, ttk.Button, "Đóng").invoke()
            except Exception as exc:
                rename_errors.append(repr(exc))
                dialog.destroy()
        window.after(50, rename_dialog_action)
        app.open_file_renamer(rename_folder, "PNG", (".png",), "png")
        assert not rename_errors, rename_errors

        # Manual metadata processing is independent of the auto-watermark state.
        from PIL.PngImagePlugin import PngInfo
        manual_source = root_path / "manual-metadata.png"
        pnginfo = PngInfo()
        pnginfo.add_text("Comment", "must be removed")
        Image.new("RGB", (64, 64), (10, 20, 30)).save(manual_source, pnginfo=pnginfo)
        manual_original = manual_source.read_bytes()
        manual_output = root_path / "Done" / "manual-metadata.cleaned.png"
        manual_errors = []
        manual_deadline = time.monotonic() + 10
        def manual_dialog_action():
            dialog = next(child for child in window.winfo_children() if isinstance(child, HiddenToplevel))
            try:
                assert dialog.state() == "withdrawn"
                start = control(dialog, ttk.Button, "Bắt đầu xử lý")
                close = control(dialog, ttk.Button, "Đóng")
                start.invoke()
                def finish_when_done():
                    try:
                        if not app.maintenance_running:
                            assert manual_output.is_file()
                            close.invoke()
                            return
                        if time.monotonic() >= manual_deadline:
                            raise AssertionError("Manual metadata UI run timed out")
                        dialog.after(50, finish_when_done)
                    except Exception as exc:
                        manual_errors.append(repr(exc))
                        dialog.destroy()
                dialog.after(50, finish_when_done)
            except Exception as exc:
                manual_errors.append(repr(exc))
                dialog.destroy()
        with (
            patch("gui.filedialog.askdirectory", return_value=str(root_path)),
            patch("gui.filedialog.askopenfilenames", return_value=(str(manual_source),)),
            patch("gui.messagebox.askyesno", return_value=True),
            patch("gui.messagebox.showinfo") as manual_notice,
            patch("gui.messagebox.showwarning") as manual_warning,
            patch("gui.messagebox.showerror") as manual_error,
        ):
            window.after(50, manual_dialog_action)
            app.open_manual_metadata_cleaner()
        assert not manual_errors, manual_errors
        assert manual_notice.called
        manual_warning.assert_not_called()
        manual_error.assert_not_called()
        assert manual_source.read_bytes() == manual_original
        with Image.open(manual_output) as cleaned:
            assert "Comment" not in cleaned.info
            assert cleaned.size == (64, 64)

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
        assert app._startup_update_checked
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
    print("GUI smoke checks passed: five palettes, three pages, hidden config/settings, rename, compression, metadata, task controls, updates, notifications; all windows withdrawn")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

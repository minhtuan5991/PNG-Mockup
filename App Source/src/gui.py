from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk
from cleanup import cleanup_app_files, format_cleanup_report
from config import PNG_IMAGE_EXTS, SUPPORTED_IMAGE_EXTS, load_config
from file_renamer import RenameConflictError, build_rename_plan, execute_rename_plan, renamed_path, renameable_files
from mockup_mode import MOCKUP_FILE_EXTS, PSD_MOCKUP_EXTS, explicit_sides_from_path
from output_metadata import SUPPORTED_METADATA_IMAGE_EXTS, clean_image_metadata_copy, cleaned_image_filename
from output_watermark import (
    OutputWatermarkError, WATERMARK_DPI, WATERMARK_SIZE,
    create_watermarked_clean_copy, inspect_watermark, watermark_output_folder_name,
)
from png_optimizer import PngImageInfo, PngProcessingResult, inspect_png, process_png_file
from queue_manager import build_queue, find_incomplete_variant_groups, load_queue, psd_files
from state import apply_state_to_queue, load_state
from task_completion import (
    CompletionNotice, RunSummary, build_completion_notice, parse_run_summary, play_system_notification,
)
from transparent_image_layout import OUTPUT_DPI, SMALL_IMAGE_LONG_EDGE_PX, next_available_output_path
from ui_themes import COLOR_PRESETS, UI_THEMES, apply_color_preset, normalize_color_preset, normalize_theme
from ui_widgets import ScrollPanel, StudioButton, StudioSection, make_icon, render_theme_skin
import update_manager
from update_manager import read_current_version

ROOT = Path(__file__).resolve().parents[1]
PYTHON_BAT = ROOT / "tools" / "python.bat"
APP_ICON = ROOT / "assets" / "app_icon.ico"
APP_LOGO = ROOT / "assets" / "logo.png"
APP_DISPLAY_NAME = "PNG Mockup"
RENAME_THUMBNAIL_CACHE_LIMIT = 512


def app_window_title(app_root: Path = ROOT) -> str:
    version = read_current_version(app_root).strip()
    return f"{APP_DISPLAY_NAME} {version if version.lower().startswith('v') else 'v' + version}"


class PngMockupGui:
    """One focused PNG/Photoshop workspace with independent app data."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(app_window_title())
        if APP_ICON.exists():
            try:
                root.iconbitmap(str(APP_ICON))
            except tk.TclError:
                pass
        width = min(1320, max(1060, root.winfo_screenwidth() - 80))
        height = min(880, max(660, root.winfo_screenheight() - 100))
        root.geometry(f"{width}x{height}")
        root.minsize(1060, 660)
        self.current_process: subprocess.Popen[str] | None = None
        self.captured_task_running = False
        self._captured_run_counter = 0
        self._active_captured_run: int | None = None
        self._force_stop_requested = False
        self._close_after_captured_run = False
        self.completion_popup: tk.Toplevel | None = None
        self.status_poll_after_id: str | None = None
        self.maintenance_running = False
        self._initialize_update_state()
        self.update_status_var = tk.StringVar(root, value="Sẽ kiểm tra kênh cập nhật riêng sau khi mở app.")
        self.update_detail_var = tk.StringVar(root, value="Việc kiểm tra và tải bản mới không khóa tác vụ PNG.")
        self.update_progress_var = tk.DoubleVar(root, value=0)
        self.update_indicator_var = tk.StringVar(root, value="")
        self.status_var = tk.StringVar(root, value="Sẵn sàng")
        self.hint_var = tk.StringVar(root, value="")
        self.progress_var = tk.DoubleVar(root, value=0)
        self.progress_text_var = tk.StringVar(root, value="0 / 0 job")
        self.count_vars = {key: tk.StringVar(root, value="0") for key in ("png", "mockup", "total", "pending", "done", "failed")}
        self.output_watermark_enabled_var = tk.BooleanVar(root, value=False)
        self.output_watermark_path_var = tk.StringVar(root, value="")
        self.output_watermark_path_display_var = tk.StringVar(root, value="Chưa chọn ảnh Watermark")
        self.output_watermark_requirement_var = tk.StringVar(root, value="PNG trong suốt · 2000 × 2000 px · 72 DPI")
        self.output_watermark_option_widgets: list[tk.Widget] = []
        self.output_watermark_choose_button: ttk.Button | None = None
        self.buttons: dict[str, StudioButton] = {}
        self.locked_buttons: set[str] = set()
        self.button_icon_refs: dict[str, ImageTk.PhotoImage] = {}
        self.brand_logo_ref: ImageTk.PhotoImage | None = None
        self._icon_specs: dict[str, tuple[ttk.Widget, str, str, int]] = {}
        self._rename_thumbnail_cache: dict[tuple[str, int, int, str, str], ImageTk.PhotoImage] = {}
        self.pages: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, StudioButton] = {}
        self.scroll_panels: list[ScrollPanel] = []
        self.settings_entries: dict[str, ttk.Entry] = {}
        self.settings_checks: dict[str, tk.BooleanVar] = {}
        self.settings_controls: list[ttk.Widget] = []
        self.palette_var = tk.StringVar(root, value="soft_purple")
        self.settings_feedback_var = tk.StringVar(root, value="")
        self.current_page = "mockup"
        try:
            config = load_config()
            self.theme_key = normalize_theme(config.ui_theme)
            self.color_preset_key = normalize_color_preset(config.ui_color_preset)
        except Exception:
            self.theme_key, self.color_preset_key = "png_modern", "soft_purple"
        self.theme = apply_color_preset(UI_THEMES[self.theme_key], self.color_preset_key)
        self._configure_style()
        self._build_ui()
        self.load_runtime_settings()
        self._load_settings_form()
        self.refresh_all()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<F5>", lambda _e: self.refresh_all())
        root.bind("<Control-Return>", self._run_shortcut)
        for key, page in (("1", "mockup"), ("2", "tools"), ("3", "settings")):
            root.bind(f"<Alt-Key-{key}>", lambda _e, p=page: self.show_page(p, focus=True))
        root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        root.bind_all("<Button-5>", self._on_mousewheel, add="+")
        root.bind("<FocusIn>", self._reveal_page_focus, add="+")
        self._update_start_after_id = root.after(600, self._maybe_auto_check_updates)

    def _configure_style(self) -> None:
        t = self.theme
        s = ttk.Style(self.root)
        if s.theme_use() != "clam":
            s.theme_use("clam")
        s.configure(".", font=("Segoe UI", 10), foreground=t.text, background=t.bg)
        for name, background in (("TFrame", t.bg), ("App.TFrame", t.bg),
                                  ("Section.TFrame", t.surface), ("Panel.TFrame", t.surface),
                                  ("Sidebar.TFrame", t.sidebar), ("Log.TFrame", t.log_bg)):
            s.configure(name, background=background)
        s.configure("Card.TFrame", background=t.surface, borderwidth=1, relief="solid",
                    bordercolor=t.border, lightcolor=t.border, darkcolor=t.border)
        for name, background, foreground, size, weight in (
            ("TLabel", t.bg, t.text, 10, "normal"),
            ("PageTitle.TLabel", t.bg, t.text, 24, "bold"),
            ("Subtitle.TLabel", t.bg, t.muted, 10, "normal"),
            ("Eyebrow.TLabel", t.bg, t.accent, 9, "bold"),
            ("Section.TLabel", t.surface, t.text, 10, "normal"),
            ("SectionMuted.TLabel", t.surface, t.muted, 10, "normal"),
            ("SectionHeading.TLabel", t.surface, t.text, 12, "bold"),
            ("CardTitle.TLabel", t.surface, t.text, 15, "bold"),
            ("CardKicker.TLabel", t.surface, t.muted, 9, "bold"),
            ("CardValue.TLabel", t.surface, t.accent, 23, "bold"),
            ("StatValue.TLabel", t.surface, t.text, 18, "bold"),
            ("SuccessValue.TLabel", t.surface, t.success, 18, "bold"),
            ("ErrorValue.TLabel", t.surface, t.danger, 18, "bold"),
            ("Sidebar.TLabel", t.sidebar, t.muted, 9, "normal"),
            ("Brand.TLabel", t.sidebar, t.text, 17, "bold"),
            ("BrandIcon.TLabel", t.sidebar, t.accent, 10, "normal"),
            ("Status.TLabel", t.selected, t.accent, 10, "bold"),
            ("LogTitle.TLabel", t.surface, t.text, 12, "bold"),
        ):
            s.configure(name, background=background, foreground=foreground, font=("Segoe UI", size, weight))
        s.configure("Status.TLabel", padding=(12, 7))
        s.configure("TButton", background=t.surface_alt, foreground=t.text, padding=(12, 9),
                    borderwidth=1, relief="flat", bordercolor=t.border, lightcolor=t.border,
                    darkcolor=t.border, focuscolor=t.accent, focusthickness=2, anchor="center")
        s.map("TButton", background=[("disabled", t.surface_alt), ("pressed", t.selected), ("active", t.selected)],
              foreground=[("disabled", t.muted)], bordercolor=[("focus", t.accent)],
              lightcolor=[("focus", t.accent)], darkcolor=[("focus", t.accent)])
        s.configure("Primary.TButton", background=t.accent, foreground=t.accent_text,
                    bordercolor=t.accent, lightcolor=t.accent, darkcolor=t.accent,
                    font=("Segoe UI", 10, "bold"), padding=(16, 10), focuscolor=t.accent_text)
        s.map("Primary.TButton", background=[("disabled", t.surface_alt), ("pressed", t.accent_hover), ("active", t.accent_hover)],
              foreground=[("disabled", t.muted)], bordercolor=[("disabled", t.border), ("focus", t.accent_hover)],
              lightcolor=[("disabled", t.border), ("focus", t.accent_hover)], darkcolor=[("disabled", t.border), ("focus", t.accent_hover)])
        s.configure("Quiet.TButton", padding=(8, 7), background=t.surface, bordercolor=t.surface,
                    lightcolor=t.surface, darkcolor=t.surface, foreground=t.muted)
        s.configure("Bold.TButton", background=t.surface_alt, foreground=t.text, padding=(12, 9),
                    borderwidth=1, relief="flat", bordercolor=t.border, lightcolor=t.border,
                    darkcolor=t.border, focuscolor=t.accent, focusthickness=2,
                    font=("Segoe UI", 10, "bold"))
        s.configure("BoldQuiet.TButton", padding=(8, 7), background=t.surface,
                    bordercolor=t.surface, lightcolor=t.surface, darkcolor=t.surface,
                    foreground=t.muted, font=("Segoe UI", 10, "bold"))
        s.configure("Danger.TButton", foreground=t.danger)
        s.map("Danger.TButton", foreground=[("disabled", t.muted), ("active", t.danger)])
        s.configure("BoldDanger.TButton", foreground=t.danger, font=("Segoe UI", 10, "bold"))
        s.map("BoldDanger.TButton", foreground=[("disabled", t.muted), ("active", t.danger)])
        s.configure("SoftGreenOutline.TButton", padding=(8, 7), background=t.surface,
                    foreground=t.muted, borderwidth=1, relief="flat",
                    bordercolor=t.soft_green_border, lightcolor=t.soft_green_border,
                    darkcolor=t.soft_green_border, focuscolor=t.accent, focusthickness=2)
        s.map("SoftGreenOutline.TButton",
              background=[("disabled", t.surface), ("pressed", t.selected), ("active", t.selected)],
              foreground=[("disabled", t.muted), ("active", t.text)],
              bordercolor=[("disabled", t.border), ("focus", t.accent)],
              lightcolor=[("disabled", t.border), ("focus", t.accent)],
              darkcolor=[("disabled", t.border), ("focus", t.accent)])
        s.configure("Nav.TButton", background=t.sidebar, foreground=t.muted, anchor="w", padding=(14, 13),
                    bordercolor=t.sidebar, lightcolor=t.sidebar, darkcolor=t.sidebar, font=("Segoe UI", 11))
        s.configure("Selected.Nav.TButton", background=t.selected, foreground=t.accent,
                    bordercolor=t.selected, lightcolor=t.selected, darkcolor=t.selected, font=("Segoe UI", 11, "bold"))
        s.map("Selected.Nav.TButton", background=[("active", t.selected), ("pressed", t.selected)])
        s.configure("TEntry", fieldbackground=t.surface_alt, foreground=t.text, insertcolor=t.text,
                    bordercolor=t.border, padding=(10, 7), lightcolor=t.border, darkcolor=t.border)
        s.map("TEntry", bordercolor=[("focus", t.accent)], lightcolor=[("focus", t.accent)], darkcolor=[("focus", t.accent)])
        s.configure("TCombobox", fieldbackground=t.surface_alt, foreground=t.text, padding=(8, 6), arrowcolor=t.muted)
        s.map("TCombobox", fieldbackground=[("readonly", t.surface_alt)], foreground=[("readonly", t.text)])
        for name in ("TCheckbutton", "TRadiobutton"):
            s.configure(name, background=t.surface, foreground=t.text, padding=(0, 5),
                        focuscolor=t.accent, indicatorbackground=t.surface_alt, indicatormargin=(0, 0, 8, 0))
            s.map(name, background=[("active", t.surface)], foreground=[("disabled", t.muted)],
                  indicatorbackground=[("selected", t.accent)])
        s.configure("TProgressbar", background=t.accent, troughcolor=t.surface_alt, borderwidth=0,
                    bordercolor=t.surface_alt, lightcolor=t.accent, darkcolor=t.accent, thickness=6)
        s.configure("TScrollbar", background=t.surface_alt, troughcolor=t.bg, arrowcolor=t.muted,
                    bordercolor=t.bg, lightcolor=t.surface_alt, darkcolor=t.surface_alt, arrowsize=12)
        s.configure("TSeparator", background=t.border)
        for name in ("TLabelframe", "Section.TLabelframe"):
            s.configure(name, background=t.surface, bordercolor=t.border, lightcolor=t.border, darkcolor=t.border)
            s.configure(name + ".Label", background=t.surface, foreground=t.text, font=("Segoe UI", 10, "bold"))
        self.root.configure(background=t.bg)
        for panel in self.scroll_panels:
            panel.canvas.configure(background=t.bg)
        if hasattr(self, "output"):
            self.output.configure(background=t.log_bg, foreground=t.log_fg, insertbackground=t.text,
                                  selectbackground=t.selected, selectforeground=t.text,
                                  highlightbackground=t.border, highlightcolor=t.accent)
        self._refresh_icons()

    def _icon(self, widget: ttk.Widget, kind: str, color: str = "accent", size: int = 20) -> None:
        self._icon_specs[str(widget)] = (widget, kind, color, size)
        self._refresh_icon(str(widget))

    def _refresh_icon(self, key: str) -> None:
        widget, kind, color, size = self._icon_specs[key]
        # Tk fonts scale in points; keep outline icons at the same relative size.
        scale = float(self.root.tk.call("tk", "scaling")) / (96 / 72)
        photo = make_icon(self.root, kind, getattr(self.theme, color), max(14, round(size * scale)))
        self.button_icon_refs[key] = photo
        widget.configure(image=photo, compound="left")

    def _refresh_icons(self) -> None:
        for key in tuple(self._icon_specs):
            widget = self._icon_specs[key][0]
            if widget.winfo_exists():
                self._refresh_icon(key)

    def apply_theme(self, theme_key: str, color_preset_key: str | None = None) -> None:
        self.theme_key = normalize_theme(theme_key)
        if color_preset_key is not None:
            self.color_preset_key = normalize_color_preset(color_preset_key)
        self.theme = apply_color_preset(UI_THEMES[self.theme_key], self.color_preset_key)
        self._rename_thumbnail_cache.clear()
        self._configure_style()
        self.root.update_idletasks()

    def _button(self, parent: tk.Misc, text: str, command: object, key: str, *,
                primary: bool = False, icon: str | None = None, lock: bool = True,
                style: str | None = None) -> StudioButton:
        button = StudioButton(parent, text=text, command=command, primary=primary, style=style)  # type: ignore[arg-type]
        self.buttons[key] = button
        if lock:
            self.locked_buttons.add(key)
        if icon:
            self._icon(button, icon, "accent_text" if primary else "accent", 18)
        return button

    def _section(self, parent: tk.Misc, title: str = "", padding: int = 16) -> StudioSection:
        return StudioSection(parent, title=title, theme=self.theme, padding=padding)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(shell, padding=(16, 24), style="Sidebar.TFrame")
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(2, weight=1)
        brand = ttk.Frame(sidebar, style="Sidebar.TFrame")
        brand.grid(row=0, column=0, sticky="ew", pady=(0, 28))
        mark = ttk.Label(brand, style="BrandIcon.TLabel")
        if APP_LOGO.is_file():
            try:
                with Image.open(APP_LOGO) as source:
                    self.brand_logo_ref = ImageTk.PhotoImage(
                        source.convert("RGB").resize((40, 40), Image.Resampling.LANCZOS),
                        master=self.root,
                    )
                mark.configure(image=self.brand_logo_ref)
            except (OSError, tk.TclError):
                self._icon(mark, "brand", size=36)
        else:
            self._icon(mark, "brand", size=36)
        mark.pack(side="left", padx=(0, 10))
        ttk.Label(brand, text="PNG\nMockup", style="Brand.TLabel").pack(side="left")
        nav = ttk.Frame(sidebar, style="Sidebar.TFrame")
        nav.grid(row=1, column=0, sticky="ew")
        for page, label, icon in (("mockup", "Tạo mockup", "layers"), ("tools", "Công cụ ảnh", "tools"), ("settings", "Cài đặt", "settings")):
            button = StudioButton(nav, text=label, command=lambda p=page: self.show_page(p), style="Nav.TButton", width=17)
            button.pack(fill="x", pady=4)
            self._icon(button, icon, "accent", 20)
            self.nav_buttons[page] = button
        footer = ttk.Frame(sidebar, style="Sidebar.TFrame")
        footer.grid(row=3, column=0, sticky="ew")
        ttk.Label(footer, text="PNG  →  PSD  →  MOCKUP", style="Sidebar.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(footer, text="Xử lý trên máy\nDữ liệu độc lập", style="Sidebar.TLabel", justify="left").pack(anchor="w", pady=(8, 18))
        ttk.Separator(footer).pack(fill="x", pady=(0, 14))
        ttk.Label(footer, text=f"Phiên bản {read_current_version(ROOT)}", style="Sidebar.TLabel").pack(anchor="w")
        ttk.Label(footer, text="Alt + 1 / 2 / 3  ·  Chuyển trang", style="Sidebar.TLabel", font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))
        self.update_indicator_button = ttk.Button(
            footer, textvariable=self.update_indicator_var,
            command=self.open_settings, style="Nav.TButton",
        )
        main = ttk.Frame(shell, padding=(24, 20, 24, 20), style="App.TFrame")
        self.main_frame = main
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        header = ttk.Frame(main, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        copy = ttk.Frame(header, style="App.TFrame")
        copy.pack(side="left", fill="x", expand=True)
        self.header_title_label = ttk.Label(copy, text="Tạo mockup", style="PageTitle.TLabel")
        self.header_title_label.pack(anchor="w")
        self.header_subtitle_label = ttk.Label(copy, text="Từ thiết kế PNG đến bộ ảnh mockup bằng Photoshop.", style="Subtitle.TLabel")
        self.header_subtitle_label.pack(anchor="w", pady=(5, 0))
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="Status.TLabel", wraplength=220)
        self.status_label.pack(side="right", anchor="n", pady=(6, 0), padx=(12, 0))
        host = ttk.Frame(main, style="App.TFrame")
        host.grid(row=1, column=0, sticky="nsew")
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)
        for key in ("mockup", "tools", "settings"):
            page = ttk.Frame(host, style="App.TFrame")
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
        self._build_mockup_page(self.pages["mockup"])
        self._build_tools_page(self.pages["tools"])
        self._build_settings_page(self.pages["settings"])
        self.show_page("mockup")

    def _source_card(self, parent: ttk.Frame, column: int, *, key: str, title: str, kicker: str, note: str) -> None:
        section = self._section(parent)
        section.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
        body = section.body
        top = ttk.Frame(body, style="Section.TFrame")
        top.pack(fill="x")
        heading = ttk.Frame(top, style="Section.TFrame")
        heading.pack(side="left", fill="x", expand=True)
        ttk.Label(heading, text=kicker, style="CardKicker.TLabel").pack(anchor="w")
        title_label = ttk.Label(heading, text=title, style="CardTitle.TLabel")
        self._icon(title_label, key, size=23)
        title_label.pack(anchor="w", pady=(5, 0))
        ttk.Label(top, textvariable=self.count_vars[key], style="CardValue.TLabel").pack(side="right", anchor="center", padx=(12, 0))
        ttk.Label(body, text=note, style="SectionMuted.TLabel").pack(anchor="w", pady=(6, 12))
        actions = ttk.Frame(body, style="Section.TFrame")
        actions.pack(fill="x")
        folder = lambda: load_config().png_dir if key == "png" else load_config().mockup_dir
        self._button(actions, "Mở PNG" if key == "png" else "Mở Mockup", lambda: self.open_path(folder()), key, icon="folder", lock=False, style="Bold.TButton").pack(side="left")
        self._button(actions, "Đổi Tên", lambda: self.open_file_renamer(folder(), "PNG" if key == "png" else "Mockup", PNG_IMAGE_EXTS if key == "png" else MOCKUP_FILE_EXTS, "png" if key == "png" else "mockup"), "rename_" + key, icon="rename", style="Bold.TButton").pack(side="left", padx=(8, 0))

    def _build_mockup_page(self, parent: ttk.Frame) -> None:
        # The log grows on large screens; a single page scroll preserves every
        # action on small laptop screens and at larger Windows text scales.
        scroll = ScrollPanel(parent, background=self.theme.bg, stretch=True)
        scroll.pack(fill="both", expand=True)
        self.scroll_panels.append(scroll)
        parent = scroll.body
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        sources = ttk.Frame(parent, style="App.TFrame")
        sources.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        sources.columnconfigure((0, 1), weight=1, uniform="sources")
        self._source_card(sources, 0, key="png", title="PNG đầu vào", kicker="01  /  THIẾT KẾ", note="Một mặt hoặc cặp mặt trước / sau .f / .b")
        self._source_card(sources, 1, key="mockup", title="PSD mockup", kicker="02  /  PHÔI MOCKUP", note="Tệp .psd / .psb có hậu tố .1S / .2S")
        section = self._section(parent, padding=16)
        section.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        body = section.body
        stats = ttk.Frame(body, style="Section.TFrame")
        stats.pack(fill="x")
        for column, (key, title, style) in enumerate((("total", "Tổng job", "StatValue.TLabel"), ("pending", "Đang chờ", "StatValue.TLabel"), ("done", "Đã xong", "SuccessValue.TLabel"), ("failed", "Lỗi", "ErrorValue.TLabel"))):
            stats.columnconfigure(column, weight=1, uniform="counts")
            cell = ttk.Frame(stats, style="Section.TFrame")
            cell.grid(row=0, column=column, sticky="ew", padx=(0, 12))
            ttk.Label(cell, textvariable=self.count_vars[key], style=style).pack(side="left")
            ttk.Label(cell, text=title, style="SectionMuted.TLabel").pack(side="left", padx=(8, 0), pady=(5, 0))
        progress_row = ttk.Frame(body, style="Section.TFrame")
        progress_row.pack(fill="x", pady=(11, 4))
        ttk.Progressbar(progress_row, variable=self.progress_var, maximum=100, mode="determinate").pack(side="left", fill="x", expand=True, pady=(4, 0))
        ttk.Label(progress_row, textvariable=self.progress_text_var, style="SectionMuted.TLabel", width=12, anchor="e").pack(side="right", padx=(14, 0))
        hint = ttk.Label(body, textvariable=self.hint_var, style="SectionMuted.TLabel", wraplength=840)
        hint.pack(fill="x", pady=(3, 12))
        hint.bind("<Configure>", lambda e: hint.configure(wraplength=max(300, e.width)))
        actions = ttk.Frame(body, style="Section.TFrame")
        actions.pack(fill="x")
        self._button(actions, "Bắt đầu / Tiếp tục", lambda: self.start_automation(False), "run", primary=True, icon="run").pack(side="left")
        self._button(actions, "Chạy thử 1 job", lambda: self.start_automation(True), "one_job", icon="one_job").pack(side="left", padx=(8, 0))
        self._button(actions, "Dừng sau job", self.request_stop, "stop", icon="stop", lock=False).pack(side="left", padx=(8, 0))
        self._button(actions, "Buộc dừng", self.force_stop, "force_stop", lock=False, style="BoldDanger.TButton").pack(side="right", padx=(8, 0))
        ttk.Separator(body).pack(fill="x", pady=(14, 8))
        watermark = ttk.Frame(body, style="Section.TFrame")
        watermark.pack(fill="x")
        check = ttk.Checkbutton(watermark, text="Watermark tự động", variable=self.output_watermark_enabled_var, command=self.on_output_watermark_toggle)
        check.pack(side="left", padx=(0, 12))
        self.output_watermark_option_widgets.append(check)
        self.output_watermark_choose_button = ttk.Button(watermark, text="Chọn PNG", command=self.choose_output_watermark, style="SoftGreenOutline.TButton")
        self.output_watermark_choose_button.pack(side="right")
        self.output_watermark_option_widgets.append(self.output_watermark_choose_button)
        path = ttk.Label(watermark, textvariable=self.output_watermark_path_display_var, style="SectionMuted.TLabel", anchor="w", width=10)
        path.pack(side="left", fill="x", expand=True)
        ttk.Label(body, textvariable=self.output_watermark_requirement_var,
                  style="SectionMuted.TLabel", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))
        self._build_log(parent)

    def _build_log(self, parent: ttk.Frame) -> None:
        section = self._section(parent, padding=14)
        section.grid(row=2, column=0, sticky="nsew")
        body = section.body
        bar = ttk.Frame(body, style="Section.TFrame")
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text="Nhật ký chạy", style="LogTitle.TLabel").pack(side="left")
        for text, command, key in (("Làm mới", self.refresh_all, "refresh_top"), ("Đặt lỗi về chờ", self.run_reset_failed, "reset_top"), ("Chạy lại lỗi", self.run_error_jobs, "run_error_top")):
            self._button(bar, text, command, key, lock=key != "refresh_top", style="SoftGreenOutline.TButton").pack(side="right", padx=(6, 0))
        text_frame = ttk.Frame(body, style="Section.TFrame")
        text_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical")
        self.output = tk.Text(text_frame, height=5, wrap="word", font=("Consolas", 10),
            yscrollcommand=scrollbar.set, borderwidth=0, relief="flat", padx=12, pady=10,
            background=self.theme.log_bg, foreground=self.theme.log_fg, selectbackground=self.theme.selected,
            selectforeground=self.theme.text, highlightthickness=1, highlightbackground=self.theme.border,
            highlightcolor=self.theme.accent, state="disabled", takefocus=True)
        scrollbar.configure(command=self.output.yview)
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        footer = ttk.Frame(body, style="Section.TFrame")
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(footer, text="Tự tiếp tục các job chưa xong · Ctrl + Enter để chạy", style="SectionMuted.TLabel", font=("Segoe UI", 9)).pack(side="left")
        self._button(footer, "Mở Output", lambda: self.open_path(load_config().output_dir), "output", icon="folder", lock=False, style="BoldQuiet.TButton").pack(side="right")

    def _build_tools_page(self, parent: ttk.Frame) -> None:
        scroll = ScrollPanel(parent, background=self.theme.bg)
        scroll.pack(fill="both", expand=True)
        self.scroll_panels.append(scroll)
        body = scroll.body
        body.columnconfigure((0, 1), weight=1, uniform="tools")
        cards = (
            ("Kích thước & nén PNG", "Đổi kích thước canvas hoặc nén lossless. Bản xử lý được lưu riêng, giữ nguyên ảnh gốc.", "Chọn ảnh PNG", self.open_batch_image_resize, "batch_resize", "resize"),
            ("Metadata & Watermark", "Xóa metadata hoặc thêm watermark cho các ảnh được chọn. Không phụ thuộc cài đặt tự động.", "Chọn ảnh để xử lý", self.open_manual_metadata_cleaner, "batch_metadata_clean", "watermark"),
            ("Đổi tên PNG", "Áp dụng .wh / .bl và .f / .b, xem trước tên mới. Không ghi đè file đã tồn tại.", "Đổi tên PNG", lambda: self.open_file_renamer(load_config().png_dir, "PNG", PNG_IMAGE_EXTS, "png"), "rename_png_tools", "image"),
            ("Đổi tên Mockup", "Chọn màu áo và số mặt .1S / .2S cho phôi PSD. Giữ nguyên phần mở rộng của file.", "Đổi tên Mockup", lambda: self.open_file_renamer(load_config().mockup_dir, "Mockup", MOCKUP_FILE_EXTS, "mockup"), "rename_mockup_tools", "layers"),
        )
        for index, (title, note, label, command, key, icon) in enumerate(cards):
            section = self._section(body, padding=20)
            section.grid(row=index // 2, column=index % 2, sticky="nsew", padx=(0, 8) if index % 2 == 0 else (8, 0), pady=(0, 16))
            card = section.body
            heading = ttk.Label(card, text=title, style="CardTitle.TLabel")
            self._icon(heading, icon, size=25)
            heading.pack(anchor="w", pady=(0, 12))
            note_label = ttk.Label(card, text=note, wraplength=355, justify="left", style="SectionMuted.TLabel")
            note_label.pack(fill="x", pady=(0, 20))
            note_label.bind("<Configure>", lambda e, label=note_label: label.configure(wraplength=max(250, e.width)))
            self._button(card, label, command, key).pack(anchor="w")
        maintenance = self._section(body, "Hàng đợi & hệ thống", padding=20)
        maintenance.grid(row=2, column=0, columnspan=2, sticky="ew")
        controls = maintenance.body
        for index, (label, command, key, icon) in enumerate((
            ("Kiểm tra hệ thống", self.run_diagnose, "diagnose", "diagnose"),
            ("Tạo lại queue", self.run_build_queue, "queue", "queue"),
            ("Dọn file tạm", self.run_cleanup, "cleanup", "cleanup"),
            ("Mở logs", lambda: self.open_path(load_config().logs_dir), "logs", "logs"),
        )):
            controls.columnconfigure(index, weight=1, uniform="maintenance")
            self._button(controls, label, command, key, icon=icon, lock=key != "logs").grid(row=0, column=index, sticky="ew", padx=(0, 8) if index < 3 else 0)
        ttk.Label(controls, text="Dọn dẹp chỉ tác động đến dữ liệu tạm của PNG Mockup; ảnh nguồn, Output và trạng thái tiếp tục được giữ lại.", style="SectionMuted.TLabel", wraplength=850).grid(row=1, column=0, columnspan=4, sticky="w", pady=(14, 0))

    def _build_settings_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        scroll = ScrollPanel(parent, background=self.theme.bg)
        scroll.grid(row=0, column=0, sticky="nsew")
        self.scroll_panels.append(scroll)
        content = scroll.body
        appearance = self._section(content, "Giao diện", padding=20)
        appearance.pack(fill="x", pady=(0, 16))
        body = appearance.body
        ttk.Label(body, text="PNG Modern", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="Một phong cách. Năm bảng màu. Chọn để xem trước, sau đó lưu cài đặt.", style="SectionMuted.TLabel").pack(anchor="w", pady=(4, 14))
        palettes = ttk.Frame(body, style="Section.TFrame")
        palettes.pack(fill="x")
        for index, (key, preset) in enumerate(COLOR_PRESETS.items()):
            palettes.columnconfigure(index, weight=1, uniform="palettes")
            radio = ttk.Radiobutton(palettes, text=preset["label"], variable=self.palette_var, value=key,
                command=lambda: self.apply_theme("png_modern", self.palette_var.get()))
            radio.grid(row=0, column=index, sticky="w", padx=(0, 12))
            self.settings_controls.append(radio)
        processing = self._section(content, "Xử lý PNG / Photoshop", padding=20)
        processing.pack(fill="x", pady=(0, 16))
        body = processing.body
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Thời gian chờ mỗi job (giây)", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 20))
        entry = ttk.Entry(body, width=14)
        entry.grid(row=0, column=1, sticky="w")
        self.settings_entries["generation_timeout_sec"] = entry
        self.settings_controls.append(entry)
        options = (("remove_output_metadata", "Xóa metadata của ảnh mockup vừa tạo"), ("rerun_done", "Chạy lại cả job đã hoàn tất"))
        for index, (key, label) in enumerate(options, 1):
            var = tk.BooleanVar(self.root)
            self.settings_checks[key] = var
            control = ttk.Checkbutton(body, text=label, variable=var)
            control.grid(row=index, column=0, columnspan=2, sticky="w", pady=(9 if index == 1 else 0, 0))
            self.settings_controls.append(control)
        ttk.Label(body, text="PNG chạy tuần tự với canvas gốc của PSD. Ảnh Output trùng tên được thay thế khi chạy lại.", style="SectionMuted.TLabel", wraplength=830).grid(row=3, column=0, columnspan=2, sticky="w", pady=(9, 0))
        watermark = self._section(content, "Quy cách ảnh Watermark tự động", padding=20)
        watermark.pack(fill="x", pady=(0, 16))
        body = watermark.body
        for col, (key, label) in enumerate((("output_width_px", "Rộng (px)"), ("output_height_px", "Cao (px)"), ("output_dpi", "DPI"))):
            body.columnconfigure(col, weight=1, uniform="dimensions")
            ttk.Label(body, text=label, style="Section.TLabel").grid(row=0, column=col, sticky="w", pady=(0, 6))
            entry = ttk.Entry(body, width=12)
            entry.grid(row=1, column=col, sticky="ew", padx=(0, 14) if col < 2 else 0)
            self.settings_entries[key] = entry
            self.settings_controls.append(entry)
        ttk.Label(body, text="Dùng để kiểm tra PNG watermark trong suốt. Các giá trị này không đổi kích thước ảnh xuất từ PSD.", style="SectionMuted.TLabel", wraplength=830).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))
        behavior = self._section(content, "Thông báo & dọn dẹp", padding=20)
        behavior.pack(fill="x", pady=(0, 16))
        for key, label in (("completion_notification_enabled", "Hiện thông báo khi hoàn tất"), ("completion_sound_enabled", "Phát âm thanh thông báo của Windows"), ("cleanup_on_exit", "Tự dọn file tạm trước khi đóng ứng dụng")):
            var = tk.BooleanVar(self.root)
            self.settings_checks[key] = var
            control = ttk.Checkbutton(behavior.body, text=label, variable=var)
            control.pack(anchor="w")
            self.settings_controls.append(control)
        about = self._section(content, "Ứng dụng & cập nhật", padding=20)
        about.pack(fill="x", pady=(0, 8))
        ttk.Label(about.body, text=f"Đang dùng {app_window_title()}", style="SectionHeading.TLabel").pack(anchor="w")
        ttk.Label(about.body, textvariable=self.update_status_var, style="Section.TLabel", wraplength=830).pack(anchor="w", pady=(10, 4))
        ttk.Label(about.body, textvariable=self.update_detail_var, style="SectionMuted.TLabel", wraplength=830).pack(anchor="w")
        self.update_progressbar = ttk.Progressbar(
            about.body, variable=self.update_progress_var, maximum=100, mode="determinate",
        )
        self.update_actions = ttk.Frame(about.body, style="Section.TFrame")
        self.update_actions.pack(fill="x", pady=(14, 0))
        self._button(self.update_actions, "Kiểm tra bản mới", self.check_for_updates, "update",
                     icon="refresh", lock=False).pack(side="left")
        self._button(self.update_actions, "Cài và mở lại", self.install_downloaded_update,
                     "install_update", icon="check", lock=False).pack(side="left", padx=(8, 0))
        ttk.Label(about.body, text="Bản mới được tải ngầm từ kênh riêng. Cài đặt chỉ bắt đầu khi các tác vụ đã xong; cấu hình và ảnh được giữ lại.",
                  style="SectionMuted.TLabel", wraplength=830).pack(anchor="w", pady=(12, 0))
        footer = ttk.Frame(parent, style="App.TFrame")
        footer.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(footer, textvariable=self.settings_feedback_var, style="Subtitle.TLabel").pack(side="left", fill="x", expand=True)
        self._button(footer, "Hoàn tác", self._restore_settings, "settings_cancel").pack(side="right", padx=(8, 0))
        self._button(footer, "Lưu cài đặt", self.save_settings, "settings", primary=True, icon="check").pack(side="right")

    def show_page(self, page: str, *, focus: bool = False) -> None:
        if page not in self.pages:
            return
        self.current_page = page
        self.pages[page].tkraise()
        titles = {"mockup": ("Tạo mockup", "Từ thiết kế PNG đến bộ ảnh mockup bằng Photoshop."),
                  "tools": ("Công cụ ảnh", "Chuẩn bị thiết kế, chuẩn hóa tên và hoàn thiện ảnh xuất."),
                  "settings": ("Cài đặt", "Tùy chỉnh không gian làm việc và các quy tắc PNG đang dùng.")}
        title, subtitle = titles[page]
        self.header_title_label.configure(text=title)
        self.header_subtitle_label.configure(text=subtitle)
        for key, button in self.nav_buttons.items():
            button.configure(style="Selected.Nav.TButton" if key == page else "Nav.TButton")
        if focus:
            self.nav_buttons[page].focus_set()

    def open_settings(self) -> None:
        self.show_page("settings")

    def _reveal_page_focus(self, _event: tk.Event) -> None:
        for panel in self.scroll_panels:
            if panel.winfo_ismapped():
                panel.after_idle(panel.reveal_focus)

    def _run_shortcut(self, _event: tk.Event) -> str:
        focus = self.root.focus_get()
        if self.current_page == "mockup" and (focus is None or focus.winfo_toplevel() == self.root) and not self.is_running():
            self.start_automation(False)
        return "break"

    def _on_mousewheel(self, event: tk.Event) -> str | None:
        target = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
        if target is None:
            return None
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        elif getattr(event, "delta", 0):
            units = -3 if event.delta > 0 else 3
        else:
            return None
        # Text handles its native wheel binding; do not scroll it a second time.
        if isinstance(target, (tk.Text, tk.Listbox, ttk.Combobox)):
            return None
        ancestor = target
        while ancestor is not None and ancestor != self.root:
            # Includes the rename grid in its modal dialog. Native list/text
            # widgets above keep their own wheel behavior without double scroll.
            if isinstance(ancestor, tk.Canvas) and ancestor.cget("yscrollcommand"):
                ancestor.yview_scroll(units, "units")
                return "break"
            ancestor = ancestor.master
        for panel in self.scroll_panels:
            if panel.winfo_ismapped() and str(target).startswith(str(panel)):
                panel.canvas.yview_scroll(units, "units")
                return "break"
        return None

    def load_runtime_settings(self) -> None:
        try:
            config = load_config()
            self.output_watermark_enabled_var.set(config.output_watermark_enabled)
            path = str(config.output_watermark_path) if config.output_watermark_path else ""
            self.output_watermark_path_var.set(path)
            self.output_watermark_path_display_var.set(Path(path).name if path else "Chưa chọn ảnh Watermark")
            self.output_watermark_requirement_var.set(f"PNG trong suốt · {config.output_width_px} × {config.output_height_px} px · {config.output_dpi} DPI")
        except Exception as exc:
            self.write_output(f"Không đọc được cài đặt Watermark: {exc}\n")
        self.update_output_watermark_controls()

    def _load_settings_form(self) -> None:
        try:
            config = load_config()
            self.palette_var.set(normalize_color_preset(config.ui_color_preset))
            for key, entry in self.settings_entries.items():
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, str(getattr(config, key)))
            for key, var in self.settings_checks.items():
                var.set(bool(getattr(config, key)))
        except Exception as exc:
            self.settings_feedback_var.set(f"Không đọc được cài đặt: {exc}")

    def _restore_settings(self) -> None:
        if self.is_running():
            return
        self._load_settings_form()
        self.apply_theme("png_modern", self.palette_var.get())
        self.settings_feedback_var.set("Đã khôi phục cài đặt đã lưu.")

    def save_settings(self) -> None:
        if self.is_running():
            messagebox.showwarning("Đang chạy", "Hãy chờ tác vụ hoàn tất trước khi lưu cài đặt.", parent=self.root)
            return
        try:
            config = load_config()
            raw = json.loads(config.config_path.read_text(encoding="utf-8-sig"))
            for key, entry in self.settings_entries.items():
                value = int(entry.get().strip())
                if value < 1:
                    raise ValueError("Thời gian chờ, kích thước và DPI phải là số nguyên lớn hơn 0.")
                raw[key] = value
            for key, var in self.settings_checks.items():
                raw[key] = bool(var.get())
            raw["ui_theme"] = "png_modern"
            raw["ui_color_preset"] = normalize_color_preset(self.palette_var.get())
            self.write_config_file(config.config_path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
            self.apply_theme("png_modern", str(raw["ui_color_preset"]))
            self.load_runtime_settings()
            self.refresh_status()
            self.settings_feedback_var.set("Đã lưu cài đặt.")
            self.write_output("\nĐã lưu cài đặt PNG Mockup.\n")
        except Exception as exc:
            self.settings_feedback_var.set("Chưa lưu. Kiểm tra lại các giá trị.")
            messagebox.showerror("Không lưu được cài đặt", str(exc), parent=self.root)

    def refresh_all(self) -> None:
        self.refresh_status()
        self.load_log_tail()
        self.update_buttons()

    def refresh_status(self) -> None:
        try:
            config = load_config()
            config.ensure_folders()
            jobs = apply_state_to_queue(config, build_queue(config, preserve_existing=True, save=False), load_state(config))
            counts = self._apply_job_progress(jobs)
            png_count = self._count_images(config.png_dir, PNG_IMAGE_EXTS)
            self.count_vars["png"].set(str(png_count))
            self.count_vars["mockup"].set(str(len(psd_files(config.mockup_dir))))
            if not png_count:
                hint = "Thêm PNG vào thư mục đầu vào và phôi PSD vào Mockup để bắt đầu."
            elif self.png_needs_psd_mockup(config):
                hint = "Cần PSD hợp lệ: tên phôi có .1S hoặc .2S trước phần mở rộng."
            elif not jobs:
                hint = "Chưa ghép được job. Kiểm tra tên PNG và PSD cùng loại số mặt."
            elif counts.get("failed"):
                hint = "Có job lỗi. Xem nhật ký, xử lý nguyên nhân rồi chọn Chạy lại lỗi."
            elif counts.get("done") == len(jobs):
                hint = "Đã hoàn tất toàn bộ job. Ảnh của bạn nằm trong thư mục Output."
            else:
                hint = "Sẵn sàng bắt đầu hoặc tiếp tục các job chưa hoàn tất."
            self.hint_var.set(hint)
            if not self.is_running():
                self.status_var.set("Sẵn sàng")
        except Exception as exc:
            self.status_var.set("Cần kiểm tra")
            self.hint_var.set("Không đọc được trạng thái. Xem chi tiết trong nhật ký.")
            self.write_output(f"Lỗi trạng thái: {exc}\n")

    def _apply_job_progress(self, jobs: list[dict[str, object]]) -> dict[str, int]:
        counts: dict[str, int] = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for job in jobs:
            status = str(job.get("status", "pending"))
            counts[status] = counts.get(status, 0) + 1
        total = len(jobs)
        done, failed = counts.get("done", 0), counts.get("failed", 0)
        processed = min(total, done + failed)
        for key, value in (("total", total), ("pending", max(0, total - processed)), ("done", done), ("failed", failed)):
            self.count_vars[key].set(str(value))
        self.progress_var.set(processed / total * 100 if total else 0)
        if hasattr(self, "progress_text_var"):
            self.progress_text_var.set(f"{processed} / {total} job")
        return counts

    def png_needs_psd_mockup(self, config: object | None = None) -> bool:
        cfg = config or load_config()
        tagged = [path for path in psd_files(cfg.mockup_dir) if explicit_sides_from_path(path) is not None]
        return self._count_images(cfg.png_dir, PNG_IMAGE_EXTS) > 0 and not tagged

    def validate_png_psd(self) -> bool:
        try:
            if not self.png_needs_psd_mockup():
                return True
            messagebox.showerror("Chưa có PSD hợp lệ", "Hãy thêm .psd / .psb vào Mockup và đặt hậu tố .1S hoặc .2S trước phần mở rộng.\n\nVí dụ: shirt.1S.psd", parent=self.root)
        except Exception as exc:
            messagebox.showerror("Không đọc được cài đặt", str(exc), parent=self.root)
        return False

    def start_automation(self, one_job: bool) -> None:
        if self.is_running():
            messagebox.showwarning("Đang chạy", "Ứng dụng đang xử lý một tác vụ khác.", parent=self.root)
            return
        if not self.confirm_incomplete_variants() or not self.validate_output_watermark() or not self.validate_png_psd():
            return
        try:
            load_config().stop_path.unlink(missing_ok=True)
        except Exception:
            pass
        command = ["cmd.exe", "/c", str(PYTHON_BAT), "src\\app.py"]
        if one_job:
            command.append("--one-job")
        self.run_captured(command, "Chạy thử 1 job" if one_job else "Tạo mockup PNG", track_jobs=True)

    def run_build_queue(self) -> None:
        if self.confirm_incomplete_variants() and self.validate_png_psd():
            self.run_captured(["cmd.exe", "/c", str(PYTHON_BAT), "src\\queue_manager.py"], "Tạo lại queue")

    def run_reset_failed(self) -> None:
        if self.validate_png_psd():
            self.run_captured(["cmd.exe", "/c", str(PYTHON_BAT), "src\\app.py", "--reset-failed"], "Đặt job lỗi về chờ")

    def run_error_jobs(self) -> None:
        if self.validate_png_psd():
            self.run_captured(["cmd.exe", "/c", str(PYTHON_BAT), "src\\app.py", "--run-failed-once"], "Chạy lại job lỗi", track_jobs=True)

    def run_cleanup(self) -> None:
        if self.is_running():
            return
        if not messagebox.askyesno("Dọn file tạm", "Xóa dữ liệu tạm do PNG Mockup tạo ra?\n\nẢnh nguồn, PSD, Output, cấu hình và trạng thái tiếp tục được giữ nguyên.", parent=self.root):
            return
        self.maintenance_running = True
        self.status_var.set("Đang dọn file tạm")
        self.update_buttons()
        def worker() -> None:
            try:
                report = cleanup_app_files(load_config())
                self.root.after(0, self.finish_cleanup, format_cleanup_report(report), bool(report.warnings))
            except Exception as exc:
                self.root.after(0, self.finish_cleanup, f"Dọn dẹp thất bại: {exc}", True)
        threading.Thread(target=worker, daemon=True).start()

    def _initialize_update_state(self) -> None:
        """Updater work has its own lifecycle; it never owns the PNG worker lock."""
        self.update_check_running = False
        self._update_thread: threading.Thread | None = None
        self._update_cancel = threading.Event()
        self._update_events: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self._update_run_counter = 0
        self._update_start_after_id: str | None = None
        self._update_poll_after_id: str | None = None
        self._startup_update_checked = False
        self._update_seen_versions: set[str] = set()
        self._available_update: update_manager.ReleaseInfo | None = None
        self._downloaded_update: Path | None = None
        self._install_after_close: Path | None = None
        self._closing = False
        self.update_popup: tk.Toplevel | None = None
        self._update_popup_message: tk.Label | None = None
        self._update_popup_install: ttk.Button | None = None

    def _maybe_auto_check_updates(self) -> None:
        self._update_start_after_id = None
        if self._startup_update_checked or self._closing:
            return
        self._startup_update_checked = True
        # Each launch checks the immutable product channel. Legacy config flags
        # and intervals must neither route nor suppress this check.
        self.check_for_updates(silent=True)

    def check_for_updates(self, silent: bool = False) -> None:
        if self._closing or self.update_check_running:
            return
        if self._update_thread is not None and self._update_thread.is_alive():
            return
        self._update_run_counter += 1
        token = self._update_run_counter
        cancel = threading.Event()
        self._update_cancel = cancel
        self.update_check_running = True
        self.update_progress_var.set(0)
        self.update_status_var.set("Đang kiểm tra kênh cập nhật riêng…")
        self.update_detail_var.set("Bạn có thể tiếp tục chạy mockup trong lúc kiểm tra.")
        self._refresh_update_controls()
        events = self._update_events

        def emit(kind: str, payload: object = None) -> None:
            events.put((token, kind, payload))

        def log(message: str) -> None:
            emit("log", str(message))

        def worker() -> None:
            last_progress_at = 0.0

            def progress(received: int, total: int) -> None:
                nonlocal last_progress_at
                now = time.monotonic()
                if not cancel.is_set() and (now - last_progress_at >= 0.15 or (total > 0 and received >= total)):
                    last_progress_at = now
                    emit("progress", (received, total))

            try:
                channel = update_manager.read_channel(ROOT, log=log)
                if cancel.is_set():
                    return
                if not channel.enabled:
                    emit("disabled", channel.reason or "Chưa cấu hình kênh cập nhật riêng cho PNG Mockup.")
                    return
                release = update_manager.check_latest_release(ROOT, cancel_event=cancel, log=log)
                if cancel.is_set():
                    return
                if release is None:
                    emit("no_update")
                    return
                emit("available", release)
                installer = update_manager.download_release(
                    release, ROOT, progress=progress, cancel_event=cancel, log=log,
                )
                if cancel.is_set():
                    return
                if installer is None:
                    emit("download_failed")
                else:
                    emit("ready", (release, Path(installer)))
            except Exception as exc:
                # Startup errors are status/log information, never a modal.
                emit("failed", str(exc))
            finally:
                emit("done")

        try:
            # A cancelled download gets to remove its partial cache before the
            # Python process exits and the launcher releases its session lock.
            self._update_thread = threading.Thread(
                target=worker, name="png-mockup-update", daemon=False,
            )
            self._update_thread.start()
        except Exception as exc:
            self._update_thread = None
            self.update_check_running = False
            self.update_status_var.set("Chưa thể kiểm tra bản cập nhật.")
            self.update_detail_var.set("App vẫn dùng bình thường. Có thể thử lại sau.")
            self.write_output(f"\n[Cập nhật] Không khởi động được kiểm tra: {exc}\n")
            self._refresh_update_controls()
            return
        self._update_poll_after_id = self.root.after(100, self._poll_update_events)

    def _poll_update_events(self) -> None:
        self._update_poll_after_id = None
        if self._closing:
            return
        # Keep the main thread responsive even if a server emits many chunks.
        for _ in range(100):
            try:
                token, kind, payload = self._update_events.get_nowait()
            except queue.Empty:
                break
            if token != self._update_run_counter:
                continue
            if kind == "log":
                self.write_output(f"[Cập nhật] {payload}\n")
            elif kind == "disabled":
                self._available_update = None
                self._downloaded_update = None
                self.update_indicator_var.set("")
                if hasattr(self, "update_indicator_button"):
                    self.update_indicator_button.pack_forget()
                self.update_status_var.set(str(payload))
                self.update_detail_var.set("Không kiểm tra cập nhật cho bản chạy này; các tính năng PNG vẫn dùng bình thường.")
            elif kind == "no_update":
                self.update_status_var.set("Chưa nhận được bản cập nhật mới.")
                self.update_detail_var.set("Sẽ kiểm tra lại vào lần mở app sau; không ảnh hưởng phiên làm việc hiện tại.")
            elif kind == "available":
                self._available_update = payload
                self._downloaded_update = None
                version = str(payload.version)
                self.update_status_var.set(f"Có bản {version} · đang tải ngầm")
                self.update_detail_var.set("Bản tải về sẽ được kiểm tra trước khi cho phép cài đặt.")
                self.update_indicator_var.set(f"Có bản mới {version}")
                if hasattr(self, "update_indicator_button"):
                    self.update_indicator_button.pack(fill="x", pady=(12, 0))
                if hasattr(self, "update_progressbar"):
                    self.update_progressbar.pack(fill="x", pady=(10, 0), before=self.update_actions)
                if version not in self._update_seen_versions:
                    self._update_seen_versions.add(version)
                    try:
                        self._show_update_notice(version)
                    except tk.TclError:
                        self._close_update_notice()
                        # The persistent Settings status/badge remains usable
                        # even if the OS cannot display a notification window.
            elif kind == "progress":
                received, total = payload
                self.update_progress_var.set(min(100.0, received / total * 100) if total else 0)
                text = (
                    f"Đang tải: {received / 1024**2:.1f} / {total / 1024**2:.1f} MB"
                    if total else f"Đang tải: {received / 1024**2:.1f} MB"
                )
                self.update_detail_var.set(text)
                if self._update_popup_message is not None:
                    self._update_popup_message.configure(text=text + ". Bạn vẫn có thể làm mockup.")
            elif kind == "ready":
                self._available_update, self._downloaded_update = payload
                self.update_progress_var.set(100)
                self.update_indicator_var.set(f"Bản {self._available_update.version} đã sẵn sàng")
                self.write_output(f"[Cập nhật] Đã tải và kiểm tra bản {self._available_update.version}. Chờ bạn chọn cài đặt.\n")
            elif kind in {"download_failed", "failed"}:
                if kind == "failed":
                    self.write_output(f"[Cập nhật] Chưa hoàn tất: {payload}\n")
                self.update_status_var.set("Chưa tải được bản mới." if self._available_update else "Chưa thể kiểm tra bản cập nhật.")
                self.update_detail_var.set("App vẫn dùng bình thường. Chọn Kiểm tra bản mới để thử lại sau.")
                if self._update_popup_message is not None:
                    self._update_popup_message.configure(text="Chưa tải xong bản mới. Bạn có thể thử lại trong Cài đặt.")
            elif kind == "done":
                self.update_check_running = False
            self._refresh_update_controls()
        if self.update_check_running or not self._update_events.empty():
            self._update_poll_after_id = self.root.after(100, self._poll_update_events)

    def _refresh_update_controls(self) -> None:
        if not hasattr(self, "update_status_var"):
            return
        checking = self.update_check_running
        closing = getattr(self, "_closing", False)
        busy = self.is_running()
        ready = self._downloaded_update is not None
        check_button = self.buttons.get("update")
        if check_button is not None:
            check_button.configure(state="disabled" if checking or closing else "normal")
        can_install = ready and not checking and not busy
        for button in (self.buttons.get("install_update"), self._update_popup_install):
            if button is not None:
                button.configure(state="normal" if can_install else "disabled")
        if ready and not checking and self._available_update is not None:
            version = self._available_update.version
            self.update_status_var.set(f"Bản {version} đã tải và kiểm tra xong.")
            detail = (
                "Chờ tác vụ hiện tại hoàn tất, sau đó chọn Cài và mở lại."
                if busy else "Chọn Cài và mở lại khi bạn sẵn sàng. Cấu hình và ảnh được giữ nguyên."
            )
            self.update_detail_var.set(detail)
            if self._update_popup_message is not None:
                self._update_popup_message.configure(text=detail)

    def _show_update_notice(self, version: str) -> None:
        self._close_update_notice()
        popup = tk.Toplevel(self.root)
        self.update_popup = popup
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(background=self.theme.accent)
        card = tk.Frame(popup, background=self.theme.surface, padx=16, pady=14)
        card.pack(fill="both", expand=True, padx=2, pady=2)
        tk.Label(card, text=f"Có PNG Mockup {version}", background=self.theme.surface,
                 foreground=self.theme.text, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
        self._update_popup_message = tk.Label(
            card, text="Đang tải bản cập nhật. Bạn có thể tiếp tục làm mockup.",
            background=self.theme.surface, foreground=self.theme.muted,
            font=("Segoe UI", 10), wraplength=340, justify="left", anchor="w",
        )
        self._update_popup_message.pack(fill="x", pady=(8, 12))
        actions = ttk.Frame(card, style="Section.TFrame")
        actions.pack(fill="x")
        self._update_popup_install = ttk.Button(
            actions, text="Cài và mở lại", command=self.install_downloaded_update,
            style="Primary.TButton", state="disabled",
        )
        self._update_popup_install.pack(side="left")
        ttk.Button(actions, text="Để sau", command=self._close_update_notice,
                   style="Quiet.TButton").pack(side="right", padx=(12, 0))
        popup.update_idletasks()
        width = max(380, min(460, popup.winfo_reqwidth()))
        popup.geometry(f"{width}x{popup.winfo_reqheight()}+{max(12, popup.winfo_screenwidth() - width - 24)}+40")
        popup.bind("<Escape>", lambda _event: self._close_update_notice())
        popup.after(12000, lambda target=popup: self._close_update_notice(target))
        # No grab, focus, lift or taskbar activation: Photoshop remains usable.

    def _close_update_notice(self, popup: tk.Toplevel | None = None) -> None:
        target = popup or self.update_popup
        if target is None:
            return
        try:
            target.destroy()
        except tk.TclError:
            pass
        if target is self.update_popup:
            self.update_popup = None
            self._update_popup_message = None
            self._update_popup_install = None

    def install_downloaded_update(self) -> None:
        if self._closing or self._downloaded_update is None:
            return
        if self.is_running() or self.update_check_running:
            self.update_detail_var.set("Chờ các tác vụ kết thúc rồi chọn Cài và mở lại; app sẽ không buộc dừng job.")
            return
        self._install_after_close = self._downloaded_update
        self.on_close()

    def _stop_update_worker(self) -> None:
        self._closing = True
        cancel = getattr(self, "_update_cancel", None)
        if cancel is not None:
            cancel.set()
        for name in ("_update_start_after_id", "_update_poll_after_id"):
            after_id = getattr(self, name, None)
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
                setattr(self, name, None)
        # Do not join a network worker on the Tk thread. It owns no Tk objects
        # and its non-daemon lifetime lets cancellation finish cache cleanup.

    def validate_output_watermark(self) -> bool:
        if not self.output_watermark_enabled_var.get():
            return True
        try:
            config = load_config()
            if config.output_watermark_path is None:
                raise OutputWatermarkError("Hãy chọn ảnh Watermark trước khi chạy.")
            inspect_watermark(config.output_watermark_path, expected_size=(config.output_width_px, config.output_height_px), expected_dpi=config.output_dpi)
            return True
        except Exception as exc:
            messagebox.showerror("Không thể gắn Watermark", str(exc), parent=self.root)
            return False

    def update_buttons(self) -> None:
        running = self.is_running()
        for key in self.locked_buttons:
            self.buttons[key].configure(state="disabled" if running else "normal")
        process = self.current_process
        can_stop = process is not None and process.poll() is None and not self._force_stop_requested
        for key in ("stop", "force_stop"):
            self.buttons[key].configure(state="normal" if can_stop else "disabled")
        for widget in self.output_watermark_option_widgets + self.settings_controls:
            widget.configure(state="disabled" if running else "normal")
        self.update_output_watermark_controls()
        self._refresh_update_controls()

    def open_path(self, path: Path) -> None:
        try:
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror("Không mở được thư mục", str(exc), parent=self.root)

    def load_log_tail(self) -> None:
        try:
            path = load_config().log_path
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                text = "\n".join(lines[-160:])
            else:
                text = "PNG Mockup đã sẵn sàng.\nThêm PNG và PSD, sau đó chọn Chạy thử 1 job để kiểm tra phôi.\nNhật ký xử lý sẽ xuất hiện tại đây."
            self.output.configure(state="normal")
            self.output.delete("1.0", "end")
            self.output.configure(state="disabled")
            self.write_output(text + "\n")
        except Exception as exc:
            self.write_output(f"Không đọc được nhật ký: {exc}\n")

    def write_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.configure(state="disabled")
        self.output.see("end")

    # PNG utilities and task lifecycle are retained below from the original
    # implementation. Their algorithms and file naming rules are unchanged.


    def _rename_thumbnail_cache_key(
        self, path: Path
    ) -> tuple[str, int, int, str, str] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (
            str(path.absolute()).casefold(),
            stat.st_size,
            stat.st_mtime_ns,
            self.theme_key,
            self.color_preset_key,
        )


    def _cache_rename_thumbnail(
        self,
        key: tuple[str, int, int, str, str] | None,
        thumbnail: ImageTk.PhotoImage,
    ) -> ImageTk.PhotoImage:
        if key is None:
            return thumbnail
        self._rename_thumbnail_cache[key] = thumbnail
        while len(self._rename_thumbnail_cache) > RENAME_THUMBNAIL_CACHE_LIMIT:
            oldest_key = next(iter(self._rename_thumbnail_cache))
            self._rename_thumbnail_cache.pop(oldest_key)
        return thumbnail


    def _transfer_rename_thumbnail_cache(self, source: Path, target: Path) -> None:
        source_path_key = str(source.absolute()).casefold()
        matching_keys = [
            key for key in self._rename_thumbnail_cache if key[0] == source_path_key
        ]
        thumbnail = None
        for key in matching_keys:
            thumbnail = self._rename_thumbnail_cache.pop(key)
        target_key = self._rename_thumbnail_cache_key(target)
        if thumbnail is not None and target_key is not None:
            self._cache_rename_thumbnail(target_key, thumbnail)


    def _rename_thumbnail_placeholder(
        self, label: str = "?"
    ) -> ImageTk.PhotoImage:
        thumbnail_size = (88, 88)
        theme = self.theme
        canvas = Image.new(
            "RGBA", thumbnail_size, self._hex_rgb(theme.surface_alt) + (255,)
        )
        tile = render_theme_skin(
            theme, "panel", "thumbnail_tile", thumbnail_size, kind="panel"
        )
        if tile is not None:
            canvas.alpha_composite(tile)
        draw = ImageDraw.Draw(canvas)
        text_box = draw.textbbox((0, 0), label)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        draw.text(
            (
                (thumbnail_size[0] - text_width) // 2,
                (thumbnail_size[1] - text_height) // 2,
            ),
            label,
            fill=self._hex_rgb(theme.muted),
        )
        return ImageTk.PhotoImage(canvas)


    def _rename_thumbnail(self, path: Path) -> ImageTk.PhotoImage:
        thumbnail_size = (88, 88)
        theme = self.theme
        cache_key = self._rename_thumbnail_cache_key(path)
        if cache_key is not None:
            cached = self._rename_thumbnail_cache.pop(cache_key, None)
            if cached is not None:
                self._rename_thumbnail_cache[cache_key] = cached
                return cached

        if path.suffix.casefold() in PSD_MOCKUP_EXTS:
            placeholder = self._rename_thumbnail_placeholder(
                path.suffix.lstrip(".").upper()
            )
            return self._cache_rename_thumbnail(cache_key, placeholder)
        try:
            with Image.open(path) as source:
                source.thumbnail(thumbnail_size, Image.Resampling.LANCZOS)
                image = source.convert("RGBA")

            canvas = Image.new("RGBA", thumbnail_size, self._hex_rgb(theme.thumb_bg) + (255,))
            tile = render_theme_skin(
                theme, "panel", "thumbnail_tile", thumbnail_size, kind="panel"
            )
            if tile is not None:
                canvas.alpha_composite(tile)
            left = (thumbnail_size[0] - image.width) // 2
            top = (thumbnail_size[1] - image.height) // 2
            canvas.alpha_composite(image, (left, top))
            return self._cache_rename_thumbnail(
                cache_key, ImageTk.PhotoImage(canvas)
            )
        except Exception:
            return self._cache_rename_thumbnail(
                cache_key, self._rename_thumbnail_placeholder()
            )


    @staticmethod
    def _hex_rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


    def update_output_watermark_controls(self) -> None:
        choose_button = self.output_watermark_choose_button
        if choose_button is None:
            return
        enabled = (
            self.output_watermark_enabled_var.get()
            and not self.is_running()
        )
        choose_button.configure(state="normal" if enabled else "disabled")


    def on_output_watermark_toggle(self) -> None:
        if self.is_running():
            self.load_runtime_settings()
            return

        if self.output_watermark_enabled_var.get():
            if not self.choose_output_watermark():
                self.output_watermark_enabled_var.set(False)
        try:
            self._save_output_watermark_settings()
        except Exception as exc:
            self.load_runtime_settings()
            messagebox.showerror(
                "Không lưu được Watermark",
                str(exc),
                parent=self.root,
            )
            return
        self.update_output_watermark_controls()


    def choose_output_watermark(self) -> bool:
        current_value = self.output_watermark_path_var.get().strip()
        current_path = Path(current_value) if current_value else None
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Chọn ảnh Watermark PNG nền trong suốt",
            initialdir=str(
                current_path.parent
                if current_path is not None and current_path.parent.exists()
                else ROOT
            ),
            filetypes=[
                ("PNG", "*.png"),
                ("Tất cả file", "*.*"),
            ],
        )
        if not selected:
            return False

        try:
            config = load_config()
            info = inspect_watermark(
                selected,
                expected_size=(config.output_width_px, config.output_height_px),
                expected_dpi=config.output_dpi,
            )
        except Exception as exc:
            messagebox.showwarning(
                "Watermark không hợp lệ",
                str(exc),
                parent=self.root,
            )
            return False

        self.output_watermark_path_var.set(str(info.path))
        self.output_watermark_path_display_var.set(info.path.name)
        self.output_watermark_enabled_var.set(True)
        try:
            self._save_output_watermark_settings()
        except Exception as exc:
            messagebox.showerror(
                "Không lưu được Watermark",
                str(exc),
                parent=self.root,
            )
            return False
        self.update_output_watermark_controls()
        return True


    def _save_output_watermark_settings(self) -> None:
        config = load_config()
        raw = json.loads(config.config_path.read_text(encoding="utf-8-sig"))
        raw["output_watermark_enabled"] = bool(
            self.output_watermark_enabled_var.get()
        )
        raw["output_watermark_path"] = self.output_watermark_path_var.get().strip()
        self.write_config_file(
            config.config_path,
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        )


    def _refresh_running_job_progress(self) -> None:
        try:
            config = load_config()
            self._apply_job_progress(load_queue(config))
        except Exception:
            # The runner writes queue.json atomically. A transient read error
            # should not interrupt the task or flood the run log.
            pass


    def _poll_running_job_progress(self) -> None:
        self.status_poll_after_id = None
        self._refresh_running_job_progress()
        if self.is_running():
            self.status_poll_after_id = self.root.after(
                400, self._poll_running_job_progress
            )


    def _start_job_progress_polling(self) -> None:
        if self.status_poll_after_id is not None:
            try:
                self.root.after_cancel(self.status_poll_after_id)
            except tk.TclError:
                pass
        self.status_poll_after_id = self.root.after(
            100, self._poll_running_job_progress
        )


    def request_stop(self) -> None:
        try:
            config = load_config()
            config.logs_dir.mkdir(parents=True, exist_ok=True)
            config.stop_path.write_text("stop requested\n", encoding="utf-8")
            self.write_output("\nĐã yêu cầu dừng sau job hiện tại.\n")
            self.status_var.set("Sẽ dừng sau job hiện tại")
        except Exception as exc:
            messagebox.showerror("Không tạo được yêu cầu dừng", str(exc))


    def force_stop(self) -> None:
        process = self.current_process
        if process is None or process.poll() is not None:
            self.write_output(
                "\nTiến trình chưa khởi động hoặc vừa kết thúc; không cần buộc dừng.\n"
            )
            return

        if not messagebox.askyesno(
            "Buộc dừng ngay",
            "Buộc dừng có thể để lại job đang chạy ở trạng thái pending/failed. Tiếp tục?",
        ):
            return

        self._force_stop_requested = True
        self.status_var.set("Đang buộc dừng")
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                detail = (result.stdout or "").strip()
                raise RuntimeError(
                    detail or f"taskkill kết thúc với mã {result.returncode}"
                )
            self.write_output("\nĐã gửi yêu cầu buộc dừng tiến trình chạy.\n")
        except Exception as exc:
            self._force_stop_requested = False
            messagebox.showerror("Không dừng được", str(exc))
        self.update_buttons()


    def run_diagnose(self) -> None:
        self.run_captured(["cmd.exe", "/c", str(PYTHON_BAT), "src\\diagnose.py"], "Kiểm tra hệ thống")


    def confirm_incomplete_variants(self) -> bool:
        try:
            groups = find_incomplete_variant_groups(load_config())
        except Exception as exc:
            messagebox.showerror("Không kiểm tra được .wh/.bl hoặc .f/.b", str(exc))
            return False

        if not groups:
            return True

        lines = []
        for group in groups[:12]:
            lines.append(
                f"- {group.folder.name}\\{group.base_name}: có {', '.join(group.present)}, "
                f"thiếu {', '.join(group.missing)}"
            )
        if len(groups) > 12:
            lines.append(f"- ... và {len(groups) - 12} nhóm khác")

        message = (
            "Phát hiện file có biến thể .wh/.bl hoặc .f/.b chưa đủ cặp:\n\n"
            + "\n".join(lines)
            + "\n\nNếu tiếp tục, các file này sẽ được coi như mẫu bình thường "
            "và mockup vào tất cả mockup cùng loại số mặt.\n\n"
            "Bạn muốn xử lý thế nào?"
        )
        return self.ask_continue_variant_warning(message)


    def ask_continue_variant_warning(self, message: str) -> bool:
        result = tk.BooleanVar(value=False)

        dialog = tk.Toplevel(self.root)
        dialog.title("Thiếu cặp .wh/.bl hoặc .f/.b")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("620x360")
        dialog.minsize(520, 300)

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Thiếu cặp biến thể .wh/.bl hoặc .f/.b", font=("Segoe UI", 12, "bold")).pack(
            anchor="w"
        )
        ttk.Label(frame, text=message, wraplength=570, justify="left").pack(
            fill="both", expand=True, pady=(12, 12)
        )

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def choose(value: bool) -> None:
            result.set(value)
            dialog.destroy()

        ttk.Button(buttons, text="Dừng lại", command=lambda: choose(False)).pack(
            side="right"
        )
        ttk.Button(
            buttons,
            text="Vẫn tiếp tục mockup",
            command=lambda: choose(True),
            style="Primary.TButton",
        ).pack(side="right", padx=(0, 8))

        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))
        dialog.wait_window()
        return bool(result.get())


    def open_file_renamer(
        self,
        folder: Path,
        folder_label: str,
        extensions: tuple[str, ...],
        rename_mode: str = "png",
    ) -> None:
        if self.is_running():
            messagebox.showwarning(
                "Đang chạy", "Hãy chờ tác vụ hiện tại hoàn tất trước khi đổi tên.",
                parent=self.root,
            )
            return
        is_mockup = rename_mode == "mockup"
        folder.mkdir(parents=True, exist_ok=True)
        files = renameable_files(folder, extensions)
        if not files:
            messagebox.showinfo(
                "Thư mục trống",
                f"Không tìm thấy file ảnh phù hợp trong thư mục {folder_label}.",
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Đổi tên file - {folder_label}")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("980x720")
        dialog.minsize(760, 600)
        dialog.configure(background=self.theme.bg)

        main = ttk.Frame(dialog, padding=16, style="App.TFrame")
        main.pack(fill="both", expand=True)
        ttk.Label(
            main,
            text=f"Đổi tên ảnh trong {folder_label}",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            main,
            text=(
                (
                    "Chọn file cần đổi tên, sau đó chọn màu áo và số mặt mockup. "
                    if is_mockup
                    else "Chọn file cần đổi tên, sau đó chọn màu áo và mặt áo phù hợp. "
                )
                + "App luôn giữ nguyên đuôi ảnh thật và không ghi đè file đã có."
            ),
            wraplength=920,
            justify="left",
        ).pack(fill="x", pady=(4, 10))

        list_header = ttk.Frame(main)
        list_header.pack(fill="x")
        file_count_var = tk.StringVar(value=f"File trong thư mục ({len(files)})")
        ttk.Label(list_header, textvariable=file_count_var).pack(
            side="left"
        )

        file_vars: list[tuple[Path, tk.BooleanVar]] = []

        def select_all(value: bool) -> None:
            for _, variable in file_vars:
                variable.set(value)
            update_all_tiles()
            update_preview()

        ttk.Button(
            list_header, text="Bỏ chọn", command=lambda: select_all(False)
        ).pack(side="right")
        ttk.Button(
            list_header, text="Chọn tất cả", command=lambda: select_all(True)
        ).pack(side="right", padx=(0, 8))

        list_shell = ttk.Frame(main, relief="solid", borderwidth=1, style="Section.TFrame")
        list_shell.pack(fill="both", expand=True, pady=(6, 12))
        canvas = tk.Canvas(
            list_shell,
            highlightthickness=0,
            height=330,
            background=self.theme.surface_alt,
        )
        scrollbar = ttk.Scrollbar(list_shell, orient="vertical", command=canvas.yview)
        file_list = tk.Frame(canvas, background=self.theme.surface_alt, padx=8, pady=8)
        list_window = canvas.create_window((0, 0), window=file_list, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        tile_widgets: list[tk.Frame] = []

        def resize_file_list(_event: tk.Event[tk.Misc]) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_list_width(event: tk.Event[tk.Misc] | None = None) -> None:
            width = event.width if event is not None else canvas.winfo_width()
            canvas.itemconfigure(list_window, width=width)
            columns = max(2, width // 160)
            for index, tile in enumerate(tile_widgets):
                tile.grid(
                    row=index // columns,
                    column=index % columns,
                    sticky="w",
                    padx=8,
                    pady=8,
                )

        file_list.bind("<Configure>", resize_file_list)
        canvas.bind("<Configure>", resize_list_width)

        light_var = tk.BooleanVar(value=False)
        dark_var = tk.BooleanVar(value=False)
        front_var = tk.BooleanVar(value=False)
        back_var = tk.BooleanVar(value=False)
        one_side_var = tk.BooleanVar(value=False)
        two_side_var = tk.BooleanVar(value=False)
        preview_var = tk.StringVar(value="Chọn file và quy tắc để xem tên mới.")
        thumbnail_refs: list[ImageTk.PhotoImage] = []
        dialog.thumbnail_refs = thumbnail_refs

        def selected_markers() -> tuple[str | None, str | None, str | None]:
            color = "bl" if light_var.get() else "wh" if dark_var.get() else None
            side = "f" if front_var.get() else "b" if back_var.get() else None
            mockup_sides = "1s" if one_side_var.get() else "2s" if two_side_var.get() else None
            return color, side, mockup_sides

        def update_preview() -> None:
            selected = [path for path, variable in file_vars if variable.get()]
            color, side, mockup_sides = selected_markers()
            if not selected:
                preview_var.set("Chọn ít nhất một file cần đổi tên.")
                return
            if is_mockup and mockup_sides is None:
                preview_var.set("Chọn Mockup 1 Mặt (.1S) hoặc Mockup 2 Mặt (.2S).")
                return
            if not is_mockup and color is None and side is None:
                preview_var.set("Chọn ít nhất một quy tắc màu áo hoặc mặt áo.")
                return
            examples = [
                f"{path.name}  →  {renamed_path(path, color, side, mockup_sides).name}"
                for path in selected[:3]
            ]
            if len(selected) > 3:
                examples.append(f"... và {len(selected) - 3} file khác")
            preview_var.set("\n".join(examples))

        def update_tile(tile: tk.Frame, variable: tk.BooleanVar) -> None:
            background = self.theme.selected if variable.get() else self.theme.surface
            tile.configure(background=background)
            for child in tile.winfo_children():
                try:
                    child.configure(background=background)
                except tk.TclError:
                    pass
            tile.configure(
                highlightbackground=self.theme.accent if variable.get() else self.theme.border,
                highlightcolor=self.theme.accent if variable.get() else self.theme.border,
            )

        def update_all_tiles() -> None:
            for tile, variable in tile_states:
                update_tile(tile, variable)

        def on_file_toggle(tile: tk.Frame, variable: tk.BooleanVar) -> None:
            update_tile(tile, variable)
            update_preview()

        def toggle_tile(tile: tk.Frame, variable: tk.BooleanVar) -> None:
            variable.set(not variable.get())
            update_tile(tile, variable)
            update_preview()

        def choose_exclusive(
            selected: tk.BooleanVar, other: tk.BooleanVar
        ) -> None:
            if selected.get():
                other.set(False)
            update_preview()

        tile_states: list[tuple[tk.Frame, tk.BooleanVar]] = []
        name_labels: list[tk.Label] = []

        def render_file_tiles() -> None:
            current_files = renameable_files(folder, extensions)
            for child in file_list.winfo_children():
                child.destroy()
            file_vars.clear()
            tile_states.clear()
            tile_widgets.clear()
            name_labels.clear()
            thumbnail_refs.clear()
            file_count_var.set(f"File trong thư mục ({len(current_files)})")

            for path in current_files:
                variable = tk.BooleanVar(value=False)
                file_vars.append((path, variable))
                thumbnail = self._rename_thumbnail(path)
                thumbnail_refs.append(thumbnail)
                tile = tk.Frame(
                    file_list,
                    width=144,
                    height=152,
                    background=self.theme.surface,
                    highlightthickness=2,
                    highlightbackground=self.theme.border,
                    highlightcolor=self.theme.border,
                )
                tile.grid_propagate(False)
                tile.pack_propagate(False)
                check = ttk.Checkbutton(
                    tile,
                    variable=variable,
                    command=lambda t=tile, v=variable: on_file_toggle(t, v),
                    style="TCheckbutton",
                    cursor="hand2",
                )
                check.place(x=4, y=4)
                image_label = tk.Label(
                    tile,
                    image=thumbnail,
                    background=self.theme.surface,
                    cursor="hand2",
                )
                image_label.pack(pady=(8, 2))
                name_label = tk.Label(
                    tile,
                    text=path.name,
                    background=self.theme.surface,
                    foreground=self.theme.text,
                    anchor="center",
                    justify="center",
                    wraplength=132,
                    cursor="hand2",
                )
                name_label.pack(fill="x", padx=4)
                for widget in (image_label, name_label):
                    widget.bind(
                        "<Button-1>",
                        lambda _event, t=tile, v=variable: toggle_tile(t, v),
                    )
                tile_states.append((tile, variable))
                tile_widgets.append(tile)
                name_labels.append(name_label)
                update_tile(tile, variable)

            resize_list_width()
            canvas.yview_moveto(0.0)
            update_preview()

        render_file_tiles()

        options = ttk.LabelFrame(main, text="Quy tắc tên file", padding=10)
        options.pack(fill="x")
        ttk.Checkbutton(
            options,
            text="Áo sáng màu (.bl)",
            variable=light_var,
            command=lambda: choose_exclusive(light_var, dark_var),
        ).grid(row=0, column=0, sticky="w", padx=(0, 24), pady=3)
        ttk.Checkbutton(
            options,
            text="Áo tối màu (.wh)",
            variable=dark_var,
            command=lambda: choose_exclusive(dark_var, light_var),
        ).grid(row=0, column=1, sticky="w", padx=(0, 24), pady=3)
        if is_mockup:
            ttk.Checkbutton(
                options,
                text="Mockup 1 Mặt (.1S)",
                variable=one_side_var,
                command=lambda: choose_exclusive(one_side_var, two_side_var),
            ).grid(row=1, column=0, sticky="w", padx=(0, 24), pady=3)
            ttk.Checkbutton(
                options,
                text="Mockup 2 Mặt (.2S)",
                variable=two_side_var,
                command=lambda: choose_exclusive(two_side_var, one_side_var),
            ).grid(row=1, column=1, sticky="w", padx=(0, 24), pady=3)
        else:
            ttk.Checkbutton(
                options,
                text="Mặt trước (.f)",
                variable=front_var,
                command=lambda: choose_exclusive(front_var, back_var),
            ).grid(row=1, column=0, sticky="w", padx=(0, 24), pady=3)
            ttk.Checkbutton(
                options,
                text="Mặt sau (.b)",
                variable=back_var,
                command=lambda: choose_exclusive(back_var, front_var),
            ).grid(row=1, column=1, sticky="w", padx=(0, 24), pady=3)

        ttk.Label(
            main,
            textvariable=preview_var,
            wraplength=920,
            justify="left",
            style="SectionMuted.TLabel",
        ).pack(fill="x", pady=(10, 8))

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(4, 0))
        renamed_any = False

        def rename_selected() -> None:
            nonlocal renamed_any
            selected = [path for path, variable in file_vars if variable.get()]
            color, side, mockup_sides = selected_markers()
            if not selected:
                messagebox.showwarning(
                    "Chưa chọn file",
                    "Hãy chọn ít nhất một file cần đổi tên.",
                    parent=dialog,
                )
                return
            if is_mockup and mockup_sides is None:
                messagebox.showwarning(
                    "Chưa chọn số mặt mockup",
                    "Hãy chọn Mockup 1 Mặt (.1S) hoặc Mockup 2 Mặt (.2S).",
                    parent=dialog,
                )
                return
            if not is_mockup and color is None and side is None:
                messagebox.showwarning(
                    "Chưa chọn quy tắc",
                    "Hãy chọn ít nhất một loại màu áo hoặc mặt áo.",
                    parent=dialog,
                )
                return
            try:
                plan = build_rename_plan(selected, color, side, mockup_sides)
                changed = execute_rename_plan(plan)
            except (RenameConflictError, ValueError, OSError) as exc:
                messagebox.showerror(
                    "Không đổi tên được",
                    str(exc),
                    parent=dialog,
                )
                return

            self.write_output(
                f"\nĐã đổi tên {changed}/{len(selected)} file trong {folder_label}.\n"
            )
            target_by_source = {
                str(operation.source.absolute()).casefold(): (
                    operation.target
                    if str(operation.source.absolute()).casefold()
                    != str(operation.target.absolute()).casefold()
                    else operation.source
                )
                for operation in plan
            }
            for index, (path, variable) in enumerate(file_vars):
                target = target_by_source.get(str(path.absolute()).casefold())
                if target is not None:
                    if path != target:
                        self._transfer_rename_thumbnail_cache(path, target)
                    file_vars[index] = (target, variable)
                    name_labels[index].configure(text=target.name)
                variable.set(False)
            update_all_tiles()
            renamed_any = renamed_any or changed > 0
            preview_var.set(
                f"Đã đổi tên {changed}/{len(selected)} file. "
                "Chọn nhóm file tiếp theo hoặc bấm Đóng khi hoàn tất."
            )

        ttk.Button(buttons, text="Đóng", command=dialog.destroy).pack(side="right")
        ttk.Button(
            buttons,
            text="Đổi tên",
            command=rename_selected,
            style="Primary.TButton",
        ).pack(side="right", padx=(0, 8))

        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_window()
        if renamed_any:
            self.refresh_status()


    def open_batch_image_resize(self) -> None:
        if self.is_running():
            messagebox.showwarning(
                "Đang chạy",
                "Hãy chờ tác vụ hiện tại hoàn tất trước khi xử lý ảnh PNG.",
            )
            return

        folder_name = filedialog.askdirectory(
            parent=self.root,
            title="Chọn thư mục chứa ảnh PNG",
        )
        if not folder_name:
            return
        folder = Path(folder_name).resolve()

        selected_names = filedialog.askopenfilenames(
            parent=self.root,
            title="Chọn ảnh PNG cần thay đổi kích thước hoặc nén",
            initialdir=str(folder),
            filetypes=[("Ảnh PNG", "*.png"), ("Tất cả file", "*.*")],
        )
        if not selected_names:
            return

        selected_paths: list[Path] = []
        outside_folder: list[Path] = []
        for raw_path in selected_names:
            path = Path(raw_path).resolve()
            if path.parent != folder:
                outside_folder.append(path)
                continue
            if path.suffix.casefold() == ".png":
                selected_paths.append(path)

        if outside_folder:
            messagebox.showwarning(
                "File không cùng thư mục",
                "Một số file được chọn ngoài thư mục ban đầu nên sẽ được bỏ qua:\n\n"
                + "\n".join(path.name for path in outside_folder[:12]),
                parent=self.root,
            )
        if not selected_paths:
            messagebox.showwarning(
                "Chưa chọn PNG",
                "Hãy chọn ít nhất một file PNG trực tiếp trong thư mục đã chọn.",
                parent=self.root,
            )
            return

        valid_infos: list[PngImageInfo] = []
        invalid_lines: list[str] = []
        for path in sorted(set(selected_paths), key=lambda item: item.name.casefold()):
            try:
                valid_infos.append(inspect_png(path))
            except (OSError, ValueError) as exc:
                invalid_lines.append(f"- {path.name}: {exc}")

        if invalid_lines:
            details = "\n".join(invalid_lines[:12])
            if len(invalid_lines) > 12:
                details += f"\n- ... và {len(invalid_lines) - 12} file khác"
            if valid_infos:
                continue_with_valid = messagebox.askyesno(
                    "Một số file không hợp lệ",
                    "Các file sau sẽ được bỏ qua vì không phải PNG tĩnh hợp lệ:\n\n"
                    + details
                    + "\n\nChỉ tiếp tục với các file hợp lệ?",
                    parent=self.root,
                )
                if not continue_with_valid:
                    return
            else:
                messagebox.showerror(
                    "Không có PNG hợp lệ",
                    "Không có file PNG tĩnh nào có thể xử lý:\n\n" + details,
                    parent=self.root,
                )
                return

        self.open_batch_resize_options(folder, valid_infos)


    def open_batch_resize_options(
        self, folder: Path, infos: list[PngImageInfo]
    ) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Thay đổi kích thước/Nén ảnh PNG")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("700x620")
        dialog.minsize(620, 500)
        dialog.configure(background=self.theme.bg)

        main = ttk.Frame(dialog, padding=16, style="App.TFrame")
        main.pack(fill="both", expand=True)
        ttk.Label(
            main,
            text="Thay đổi kích thước/Nén ảnh PNG",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            main,
            text=(
                f"Đã chọn {len(infos)} ảnh PNG từ thư mục:\n{folder}\n"
                "Ảnh gốc luôn được giữ nguyên. Chọn một kích thước, Nén ảnh PNG, "
                "hoặc kết hợp cả hai."
            ),
            justify="left",
            wraplength=650,
        ).pack(fill="x", pady=(6, 10))

        layout_4500_var = tk.BooleanVar(value=False)
        layout_4200_var = tk.BooleanVar(value=False)
        compress_png_var = tk.BooleanVar(value=False)
        list_shell = ttk.Frame(
            main, relief="solid", borderwidth=1, style="Section.TFrame"
        )
        list_shell.pack(fill="both", expand=True, pady=(0, 12))
        file_list = tk.Listbox(
            list_shell,
            height=10,
            activestyle="none",
            background=self.theme.surface_alt,
            foreground=self.theme.text,
            selectbackground=self.theme.selected,
            selectforeground=self.theme.text,
            borderwidth=0,
            highlightthickness=0,
        )
        list_scroll = ttk.Scrollbar(
            list_shell, orient="vertical", command=file_list.yview
        )
        file_list.configure(yscrollcommand=list_scroll.set)
        file_list.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        list_scroll.pack(side="right", fill="y", pady=6)
        for info in infos:
            file_list.insert(
                "end",
                f"{info.path.name}    {info.width}x{info.height} px"
                + (
                    "  [CHỈ NÉN]"
                    if not info.can_resize_transparent
                    else "  [NHỎ]"
                    if info.long_edge < SMALL_IMAGE_LONG_EDGE_PX
                    else ""
                ),
            )

        small_infos = [
            info
            for info in infos
            if info.can_resize_transparent
            and info.long_edge < SMALL_IMAGE_LONG_EDGE_PX
        ]
        compression_only_infos = [
            info for info in infos if not info.can_resize_transparent
        ]
        if small_infos or compression_only_infos:
            notes: list[str] = []
            if small_infos:
                notes.append(
                    f"{len(small_infos)} ảnh có cạnh dài nhất nhỏ hơn "
                    f"{SMALL_IMAGE_LONG_EDGE_PX} px."
                )
            if compression_only_infos:
                notes.append(
                    f"{len(compression_only_infos)} ảnh không có vùng trong suốt hợp lệ "
                    "và chỉ có thể nén."
                )
            ttk.Label(
                main,
                text="Lưu ý: " + " ".join(notes),
                wraplength=650,
                justify="left",
                style="SectionMuted.TLabel",
            ).pack(fill="x", pady=(0, 10))

        options = ttk.LabelFrame(
            main, text="Tùy chọn xử lý PNG", padding=10
        )
        options.pack(fill="x", pady=(0, 12))
        output_folder_var = tk.StringVar(
            value="Chọn ít nhất một kích thước hoặc Nén ảnh PNG."
        )

        def selected_layout() -> str | None:
            if layout_4500_var.get():
                return "4500x5400"
            if layout_4200_var.get():
                return "4200x4800"
            return None

        def update_output_hint() -> None:
            layout = selected_layout()
            compress = bool(compress_png_var.get())
            if layout:
                suffix = " (resize + nén lossless)" if compress else " (chỉ resize)"
                output_folder_var.set(f"Output: {folder / f'Done {layout}'}{suffix}")
            elif compress:
                output_folder_var.set(f"Output: {folder / 'Done Nén PNG'} (chỉ nén)")
            else:
                output_folder_var.set(
                    "Chọn ít nhất một kích thước hoặc Nén ảnh PNG."
                )

        def choose_layout(value: str) -> None:
            if value == "4500x5400" and layout_4500_var.get():
                layout_4200_var.set(False)
            elif value == "4200x4800" and layout_4200_var.get():
                layout_4500_var.set(False)
            update_output_hint()

        ttk.Checkbutton(
            options,
            text="4500x5400",
            variable=layout_4500_var,
            command=lambda: choose_layout("4500x5400"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 28))
        ttk.Checkbutton(
            options,
            text="4200x4800",
            variable=layout_4200_var,
            command=lambda: choose_layout("4200x4800"),
        ).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(
            options,
            text="Nén ảnh PNG",
            variable=compress_png_var,
            command=update_output_hint,
        ).grid(row=0, column=2, sticky="w", padx=(28, 0))
        ttk.Label(
            options,
            text=(
                "Hai kích thước loại trừ nhau. Nén dùng oxipng lossless, giữ nguyên "
                "pixel, màu, ICC và DPI; không dùng giảm màu hoặc quantization."
            ),
            wraplength=600,
            justify="left",
            style="SectionMuted.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(
            options,
            text="Resize PNG trong suốt: căn giữa, cách mép trên 25 px, output 300 DPI.",
            wraplength=600,
            justify="left",
            style="SectionMuted.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))
        ttk.Label(
            options,
            textvariable=output_folder_var,
            wraplength=600,
            justify="left",
            style="SectionMuted.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))

        progress_var = tk.DoubleVar(value=0)
        progress_text = tk.StringVar(value="Chưa bắt đầu")
        ttk.Label(main, textvariable=progress_text, style="SectionMuted.TLabel").pack(
            fill="x", pady=(0, 4)
        )
        progress = ttk.Progressbar(
            main, variable=progress_var, maximum=len(infos), mode="determinate"
        )
        progress.pack(fill="x", pady=(0, 10))

        buttons = ttk.Frame(main, style="App.TFrame")
        buttons.pack(fill="x")

        def close_resize_dialog() -> None:
            if self.maintenance_running:
                messagebox.showinfo(
                    "Đang xử lý",
                    "Hãy chờ xử lý PNG hoàn tất trước khi đóng cửa sổ này.",
                    parent=dialog,
                )
                return
            dialog.destroy()

        close_button = ttk.Button(buttons, text="Đóng", command=close_resize_dialog)
        close_button.pack(side="right")
        output_dir_holder: dict[str, Path | None] = {"path": None}

        def open_completed_output() -> None:
            output_dir = output_dir_holder["path"]
            if output_dir is not None:
                self.open_path(output_dir)

        open_done_button = ttk.Button(
            buttons,
            text="Mở thư mục kết quả",
            command=open_completed_output,
            state="disabled",
        )
        open_done_button.pack(side="right", padx=(0, 8))

        def start_processing() -> None:
            if self.is_running():
                messagebox.showwarning(
                    "Đang chạy",
                    "Một tác vụ khác đã bắt đầu. Hãy chờ tác vụ đó hoàn tất rồi thử lại.",
                    parent=dialog,
                )
                return

            output_layout = selected_layout()
            compress = bool(compress_png_var.get())
            if output_layout is None and not compress:
                messagebox.showwarning(
                    "Chưa chọn tùy chọn",
                    "Hãy chọn một kích thước hoặc bật Nén ảnh PNG.",
                    parent=dialog,
                )
                return

            if output_layout is not None and compression_only_infos:
                blocked_names = "\n".join(
                    f"- {info.path.name}" for info in compression_only_infos[:12]
                )
                if len(compression_only_infos) > 12:
                    blocked_names += (
                        f"\n- ... và {len(compression_only_infos) - 12} file khác"
                    )
                messagebox.showwarning(
                    "Một số ảnh chỉ có thể nén",
                    "Resize yêu cầu PNG có nền trong suốt và còn nội dung hiển thị. "
                    "Các file sau chỉ có thể dùng với tùy chọn Nén ảnh PNG:\n\n"
                    + blocked_names
                    + "\n\nHãy bỏ chọn kích thước hoặc chọn lại chỉ các PNG nền trong suốt.",
                    parent=dialog,
                )
                return

            if output_layout is not None and small_infos:
                small_names = "\n".join(
                    f"- {info.path.name} ({info.width}x{info.height})"
                    for info in small_infos[:12]
                )
                if len(small_infos) > 12:
                    small_names += f"\n- ... và {len(small_infos) - 12} file khác"
                if not messagebox.askyesno(
                    "Ảnh có kích thước nhỏ",
                    f"Các ảnh sau có cạnh dài nhất nhỏ hơn {SMALL_IMAGE_LONG_EDGE_PX} px:\n\n"
                    + small_names
                    + "\n\nTiếp tục thay đổi kích thước?",
                    parent=dialog,
                ):
                    return

            start_button.configure(state="disabled")
            close_button.configure(state="disabled")
            self.maintenance_running = True
            self.status_var.set("Đang xử lý ảnh PNG")
            self.update_buttons()
            output_dir = (
                folder / f"Done {output_layout}"
                if output_layout is not None
                else folder / "Done Nén PNG"
            )
            output_dir_holder["path"] = output_dir
            worker_events: queue.Queue[tuple[str, object]] = queue.Queue()

            def worker() -> None:
                outputs: list[PngProcessingResult] = []
                failures: list[tuple[Path, str]] = []
                for index, info in enumerate(infos, start=1):
                    try:
                        destination = next_available_output_path(
                            output_dir, info.path.name
                        )
                        result = process_png_file(
                            info.path,
                            destination,
                            output_layout=output_layout,
                            compress=compress,
                            dpi=OUTPUT_DPI,
                        )
                        outputs.append(result)
                        message = f"Đã xử lý {index}/{len(infos)}: {info.path.name}"
                    except Exception as exc:
                        failures.append((info.path, str(exc)))
                        message = f"Lỗi {index}/{len(infos)}: {info.path.name}"
                    worker_events.put(("progress", (index, message)))
                worker_events.put(("done", (outputs, failures)))

            def poll_worker_events() -> None:
                finished = False
                while True:
                    try:
                        event_name, payload = worker_events.get_nowait()
                    except queue.Empty:
                        break
                    if event_name == "progress":
                        value, message = payload
                        progress_var.set(value)
                        progress_text.set(message)
                    elif event_name == "done":
                        outputs, failures = payload
                        finish_processing(
                            outputs,
                            failures,
                            output_layout,
                            compress,
                            output_dir,
                        )
                        finished = True
                        break
                if not finished and dialog.winfo_exists():
                    dialog.after(50, poll_worker_events)

            try:
                processing_thread = threading.Thread(target=worker, daemon=True)
                processing_thread.start()
            except (RuntimeError, OSError) as exc:
                self.maintenance_running = False
                self.status_var.set("Sẵn sàng")
                self.update_buttons()
                start_button.configure(state="normal")
                close_button.configure(state="normal")
                messagebox.showerror(
                    "Không bắt đầu được xử lý PNG",
                    f"Không tạo được tiến trình nền để xử lý ảnh:\n\n{exc}",
                    parent=dialog,
                )
                return
            dialog.after(50, poll_worker_events)

        def finish_processing(
            outputs: list[PngProcessingResult],
            failures: list[tuple[Path, str]],
            output_layout: str | None,
            compress: bool,
            output_dir: Path,
        ) -> None:
            self.maintenance_running = False
            self.status_var.set("Sẵn sàng")
            self.update_buttons()
            close_button.configure(state="normal")
            open_done_button.configure(state="normal" if outputs else "disabled")
            start_button.configure(state="normal")
            mode_text = (
                f"resize {output_layout} + nén lossless"
                if output_layout and compress
                else f"resize {output_layout}"
                if output_layout
                else "nén lossless"
            )
            saved_bytes = sum(item.saved_bytes for item in outputs)
            saved_text = (
                f" Đã giảm {saved_bytes / 1024**2:.2f} MB."
                if compress
                else ""
            )
            progress_text.set(
                f"Hoàn tất: {len(outputs)}/{len(infos)} ảnh, {mode_text}." + saved_text
            )
            self.write_output(
                f"\n--- Thay đổi kích thước/Nén ảnh PNG ---\n"
                f"Đã xử lý: {len(outputs)}/{len(infos)} | Chế độ: {mode_text}"
                + (f" | Đã giảm: {saved_bytes / 1024**2:.2f} MB" if compress else "")
                + f"\nThư mục output: {output_dir}\n"
            )
            if failures:
                failure_text = "\n".join(
                    f"- {path.name}: {error}" for path, error in failures[:12]
                )
                if len(failures) > 12:
                    failure_text += f"\n- ... và {len(failures) - 12} lỗi khác"
                self.write_output(f"File lỗi:\n{failure_text}\n")
                messagebox.showwarning(
                    "Xử lý PNG hoàn tất với lỗi",
                    f"Đã xử lý {len(outputs)}/{len(infos)} ảnh.\n\n"
                    + failure_text,
                    parent=dialog,
                )
            else:
                messagebox.showinfo(
                    "Xử lý PNG hoàn tất",
                    f"Đã tạo {len(outputs)} file mới trong:\n"
                    f"{output_dir}"
                    + (
                        f"\n\nDung lượng giảm: {saved_bytes / 1024**2:.2f} MB"
                        if compress
                        else ""
                    ),
                    parent=dialog,
                )

        start_button = ttk.Button(
            buttons,
            text="Bắt đầu xử lý",
            command=start_processing,
            style="Primary.TButton",
        )
        start_button.pack(side="right", padx=(0, 8))

        dialog.bind("<Escape>", lambda _event: close_resize_dialog())
        dialog.protocol("WM_DELETE_WINDOW", close_resize_dialog)
        dialog.wait_window()


    def open_manual_metadata_cleaner(self) -> None:
        """
        Process explicitly selected images without consulting app settings.

        Metadata-only output is written to Done. Watermarked output is written
        to a folder named after the selected Watermark. Originals remain
        untouched in both modes.
        """

        if self.is_running():
            messagebox.showwarning(
                "Đang chạy",
                "Hãy chờ tác vụ hiện tại hoàn tất trước khi xử lý ảnh.",
                parent=self.root,
            )
            return

        folder_name = filedialog.askdirectory(
            parent=self.root,
            title="Chọn thư mục chứa ảnh cần xử lý",
        )
        if not folder_name:
            return
        folder = Path(folder_name).resolve()

        selected_names = filedialog.askopenfilenames(
            parent=self.root,
            title="Chọn các ảnh cần xử lý",
            initialdir=str(folder),
            filetypes=[
                ("Ảnh hỗ trợ", "*.png *.jpg *.jpeg *.webp"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg *.jpeg"),
                ("WebP", "*.webp"),
                ("Tất cả file", "*.*"),
            ],
        )
        if not selected_names:
            return

        selected_paths: list[Path] = []
        outside_folder: list[Path] = []
        unsupported: list[Path] = []
        for raw_path in selected_names:
            path = Path(raw_path).resolve()
            if path.parent != folder:
                outside_folder.append(path)
            elif path.suffix.casefold() not in SUPPORTED_METADATA_IMAGE_EXTS:
                unsupported.append(path)
            else:
                selected_paths.append(path)

        skipped = outside_folder + unsupported
        if skipped:
            detail_lines = [
                f"- {path.name}"
                + (
                    " (ngoài thư mục đã chọn)"
                    if path in outside_folder
                    else " (định dạng không hỗ trợ)"
                )
                for path in skipped[:12]
            ]
            if len(skipped) > 12:
                detail_lines.append(f"- ... và {len(skipped) - 12} file khác")
            messagebox.showwarning(
                "Một số file được bỏ qua",
                "Công cụ hỗ trợ PNG, JPG, JPEG và WebP nằm trực tiếp trong "
                "thư mục đã chọn:\n\n" + "\n".join(detail_lines),
                parent=self.root,
            )

        selected_paths = sorted(
            set(selected_paths),
            key=lambda item: item.name.casefold(),
        )
        if not selected_paths:
            messagebox.showwarning(
                "Chưa chọn ảnh hợp lệ",
                "Hãy chọn ít nhất một ảnh PNG, JPG, JPEG hoặc WebP trực tiếp "
                "trong thư mục đã chọn.",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Xóa Metadata/Thêm Watermark")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("720x470")
        dialog.minsize(640, 420)
        dialog.configure(background=self.theme.bg)

        main = ttk.Frame(dialog, padding=14, style="App.TFrame")
        main.pack(fill="both", expand=True)
        ttk.Label(
            main,
            text=f"Đã chọn {len(selected_paths)} ảnh trong: {folder}",
            wraplength=670,
            justify="left",
            style="Section.TLabel",
        ).pack(fill="x", pady=(0, 10))

        progress_var = tk.DoubleVar(value=0)
        progress_text = tk.StringVar(
            value=f"Chưa bắt đầu — 0/{len(selected_paths)} ảnh"
        )
        ttk.Label(
            main,
            textvariable=progress_text,
            style="SectionMuted.TLabel",
        ).pack(fill="x", pady=(0, 4))
        ttk.Progressbar(
            main,
            variable=progress_var,
            maximum=len(selected_paths),
            mode="determinate",
        ).pack(fill="x", pady=(0, 12))

        options = ttk.LabelFrame(
            main,
            text="Tác vụ",
            padding=12,
        )
        options.pack(fill="x", pady=(0, 12))
        metadata_var = tk.BooleanVar(value=True)
        watermark_var = tk.BooleanVar(value=False)
        watermark_path_var = tk.StringVar(value="")
        output_folder_var = tk.StringVar(
            value=f"Thư mục kết quả: {folder / 'Done'}"
        )

        def enforce_metadata_selection() -> None:
            if watermark_var.get():
                metadata_var.set(True)

        metadata_check = ttk.Checkbutton(
            options,
            text="Xóa Metadata",
            variable=metadata_var,
            command=enforce_metadata_selection,
        )
        metadata_check.grid(row=0, column=0, sticky="w", padx=(0, 28))
        watermark_check = ttk.Checkbutton(
            options,
            text="Gắn Watermark",
            variable=watermark_var,
        )
        watermark_check.grid(row=0, column=1, sticky="w")

        watermark_path_frame = ttk.LabelFrame(
            main,
            text="Chọn Watermark",
            padding=10,
        )
        watermark_path_frame.pack(fill="x", pady=(0, 10))
        watermark_entry = ttk.Entry(
            watermark_path_frame,
            textvariable=watermark_path_var,
            state="disabled",
        )
        watermark_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        watermark_button = ttk.Button(
            watermark_path_frame,
            text="Chọn file PNG",
        )
        watermark_button.pack(side="right")
        ttk.Label(
            main,
            textvariable=output_folder_var,
            wraplength=670,
            justify="left",
            style="SectionMuted.TLabel",
        ).pack(fill="x", pady=(0, 6))
        ttk.Label(
            main,
            text=(
                "Watermark phải là PNG nền trong suốt 2000x2000 px / 72 DPI. "
                "Khi bật Watermark, Xóa Metadata được bật bắt buộc và chạy sau cùng."
            ),
            wraplength=670,
            justify="left",
            style="SectionMuted.TLabel",
        ).pack(fill="x", pady=(0, 10))

        buttons = ttk.Frame(main, style="App.TFrame")
        buttons.pack(fill="x", side="bottom")
        output_dir_holder: dict[str, Path | None] = {"path": None}

        def update_manual_controls() -> None:
            watermark_selected = watermark_var.get()
            metadata_check.configure(
                state="disabled" if watermark_selected else "normal"
            )
            watermark_entry.configure(
                state="readonly" if watermark_selected else "disabled"
            )
            watermark_button.configure(
                state="normal" if watermark_selected else "disabled"
            )
            if watermark_selected and watermark_path_var.get().strip():
                target = folder / watermark_output_folder_name(
                    watermark_path_var.get()
                )
            else:
                target = folder / "Done"
            output_folder_var.set(f"Thư mục kết quả: {target}")

        def choose_manual_watermark() -> bool:
            current = watermark_path_var.get().strip()
            initial_folder = (
                Path(current).parent
                if current and Path(current).parent.exists()
                else folder
            )
            selected = filedialog.askopenfilename(
                parent=dialog,
                title="Chọn Watermark PNG 2000x2000 px / 72 DPI",
                initialdir=str(initial_folder),
                filetypes=[("PNG", "*.png"), ("Tất cả file", "*.*")],
            )
            if not selected:
                return False
            try:
                info = inspect_watermark(
                    selected,
                    expected_size=WATERMARK_SIZE,
                    expected_dpi=WATERMARK_DPI,
                )
            except Exception as exc:
                messagebox.showwarning(
                    "Watermark không hợp lệ",
                    str(exc),
                    parent=dialog,
                )
                return False
            watermark_path_var.set(str(info.path))
            update_manual_controls()
            return True

        def on_manual_watermark_toggle() -> None:
            if watermark_var.get():
                metadata_var.set(True)
                if not choose_manual_watermark():
                    watermark_var.set(False)
            update_manual_controls()

        watermark_check.configure(command=on_manual_watermark_toggle)
        watermark_button.configure(command=choose_manual_watermark)
        update_manual_controls()

        def close_dialog() -> None:
            if self.maintenance_running:
                messagebox.showinfo(
                    "Đang xử lý",
                    "Hãy chờ công việc hoàn tất trước khi đóng cửa sổ này.",
                    parent=dialog,
                )
                return
            dialog.destroy()

        close_button = ttk.Button(
            buttons,
            text="Đóng",
            command=close_dialog,
        )
        close_button.pack(side="right")
        open_done_button = ttk.Button(
            buttons,
            text="Mở thư mục kết quả",
            state="disabled",
            command=lambda: (
                self.open_path(output_dir_holder["path"])
                if output_dir_holder["path"] is not None
                else None
            ),
        )
        open_done_button.pack(side="right", padx=(0, 8))

        def finish_processing(
            outputs: list[Path],
            failures: list[tuple[Path, str]],
            output_dir: Path,
            used_watermark: bool,
        ) -> None:
            self.maintenance_running = False
            self.status_var.set("Sẵn sàng")
            self.update_buttons()
            progress_var.set(len(selected_paths))
            progress_text.set(
                f"Hoàn tất — {len(outputs)}/{len(selected_paths)} ảnh thành công"
            )
            close_button.configure(state="normal")
            open_done_button.configure(state="normal")
            start_button.configure(state="normal")
            watermark_check.configure(state="normal")
            if used_watermark:
                metadata_check.configure(state="disabled")
                watermark_button.configure(state="normal")
                watermark_entry.configure(state="readonly")
            else:
                metadata_check.configure(state="normal")
                watermark_button.configure(state="disabled")
                watermark_entry.configure(state="disabled")

            action_label = (
                "Gắn Watermark + Xóa Metadata"
                if used_watermark
                else "Xóa Metadata"
            )
            self.write_output(
                f"\n--- {action_label} thủ công ---\n"
                f"Hoàn tất: {len(outputs)}/{len(selected_paths)} ảnh. "
                f"Ảnh gốc được giữ nguyên.\nKết quả: {output_dir}\n"
            )
            if failures:
                failure_text = "\n".join(
                    f"- {path.name}: {error}"
                    for path, error in failures[:12]
                )
                if len(failures) > 12:
                    failure_text += (
                        f"\n- ... và {len(failures) - 12} lỗi khác"
                    )
                self.write_output(f"File lỗi:\n{failure_text}\n")
                messagebox.showwarning(
                    "Xử lý ảnh hoàn tất với lỗi",
                    f"Đã tạo {len(outputs)}/{len(selected_paths)} ảnh.\n\n"
                    + failure_text
                    + f"\n\nKết quả: {output_dir}",
                    parent=dialog,
                )
                return

            messagebox.showinfo(
                "Xử lý ảnh hoàn tất",
                f"Đã tạo {len(outputs)} ảnh trong:\n{output_dir}",
                parent=dialog,
            )

        def start_processing() -> None:
            if self.is_running():
                messagebox.showwarning(
                    "Đang chạy",
                    "Hãy chờ tác vụ hiện tại hoàn tất trước khi xử lý ảnh.",
                    parent=dialog,
                )
                return
            remove_metadata = metadata_var.get()
            use_watermark = watermark_var.get()
            if not remove_metadata and not use_watermark:
                messagebox.showwarning(
                    "Chưa chọn tác vụ",
                    "Hãy chọn Xóa Metadata hoặc Gắn Watermark.",
                    parent=dialog,
                )
                return

            selected_watermark: Path | None = None
            if use_watermark:
                metadata_var.set(True)
                remove_metadata = True
                try:
                    selected_watermark = inspect_watermark(
                        watermark_path_var.get(),
                        expected_size=WATERMARK_SIZE,
                        expected_dpi=WATERMARK_DPI,
                    ).path
                except Exception as exc:
                    messagebox.showerror(
                        "Watermark không hợp lệ",
                        str(exc),
                        parent=dialog,
                    )
                    return
                output_dir = folder / watermark_output_folder_name(
                    selected_watermark
                )
            else:
                output_dir = folder / "Done"

            action_text = (
                "gắn Watermark, sau đó xóa Metadata"
                if use_watermark
                else "xóa Metadata"
            )
            if not messagebox.askyesno(
                "Xác nhận xử lý ảnh",
                f"Đã chọn {len(selected_paths)} ảnh để {action_text}.\n\n"
                "Bước Xóa Metadata sẽ loại bỏ đầy đủ Comment, EXIF, XMP, "
                "EXIF thumbnail, IPTC và ICC profile. Ảnh gốc được giữ nguyên; "
                "file kết quả có đuôi .cleaned và được lưu tại:\n"
                f"{output_dir}\n\nTiếp tục?",
                parent=dialog,
            ):
                return

            output_dir_holder["path"] = output_dir
            start_button.configure(state="disabled")
            close_button.configure(state="disabled")
            open_done_button.configure(state="disabled")
            metadata_check.configure(state="disabled")
            watermark_check.configure(state="disabled")
            watermark_button.configure(state="disabled")
            watermark_entry.configure(state="disabled")
            self.maintenance_running = True
            self.status_var.set("Đang xử lý ảnh đã chọn")
            self.update_buttons()
            progress_var.set(0)
            progress_text.set(
                f"Đang chuẩn bị — 0/{len(selected_paths)} ảnh"
            )
            worker_events: queue.Queue[tuple[str, object]] = queue.Queue()

            def worker() -> None:
                outputs: list[Path] = []
                failures: list[tuple[Path, str]] = []
                for index, source in enumerate(selected_paths, start=1):
                    try:
                        cleaned_name = cleaned_image_filename(source)
                        destination = next_available_output_path(
                            output_dir,
                            cleaned_name,
                        )
                        if use_watermark and selected_watermark is not None:
                            create_watermarked_clean_copy(
                                source,
                                destination,
                                selected_watermark,
                            )
                        else:
                            clean_image_metadata_copy(source, destination)
                        outputs.append(destination)
                        status = (
                            f"Đã xử lý {index}/{len(selected_paths)}: "
                            f"{source.name}"
                        )
                    except Exception as exc:
                        failures.append((source, str(exc)))
                        status = (
                            f"Lỗi {index}/{len(selected_paths)}: "
                            f"{source.name}"
                        )
                    worker_events.put(("progress", (index, status)))
                worker_events.put(("done", (outputs, failures)))

            def poll_worker_events() -> None:
                while True:
                    try:
                        event_name, payload = worker_events.get_nowait()
                    except queue.Empty:
                        break
                    if event_name == "progress":
                        value, status = payload
                        progress_var.set(value)
                        progress_text.set(status)
                        self.status_var.set(status)
                    elif event_name == "done":
                        outputs, failures = payload
                        finish_processing(outputs, failures, output_dir, use_watermark)
                        return
                if dialog.winfo_exists():
                    dialog.after(50, poll_worker_events)

            try:
                threading.Thread(target=worker, daemon=True).start()
            except (RuntimeError, OSError) as exc:
                self.maintenance_running = False
                self.status_var.set("Sẵn sàng")
                self.update_buttons()
                start_button.configure(state="normal")
                close_button.configure(state="normal")
                watermark_check.configure(state="normal")
                update_manual_controls()
                messagebox.showerror(
                    "Không bắt đầu được xử lý ảnh", str(exc), parent=dialog,
                )
                return
            dialog.after(50, poll_worker_events)

        start_button = ttk.Button(
            buttons,
            text="Bắt đầu xử lý",
            command=start_processing,
            style="Primary.TButton",
        )
        start_button.pack(side="right", padx=(0, 8))

        dialog.bind("<Escape>", lambda _event: close_dialog())
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.wait_window()


    def finish_cleanup(self, message: str, has_warnings: bool) -> None:
        self.maintenance_running = False
        self.write_output(f"\n--- Dọn dẹp File Rác ---\n{message}\n")
        self.refresh_all()
        if has_warnings:
            messagebox.showwarning("Dọn dẹp hoàn tất với cảnh báo", message)
        else:
            messagebox.showinfo("Dọn dẹp hoàn tất", message)


    def on_close(self) -> None:
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_install_after_close", None) is not None and self.is_running():
            # The updater must never take the force-stop path below, including
            # a job that started between an install click and this callback.
            self._install_after_close = None
            self.update_detail_var.set("Chờ tác vụ hiện tại hoàn tất rồi chọn Cài và mở lại.")
            return
        if self.maintenance_running:
            messagebox.showinfo(
                "Đang xử lý",
                "Hãy chờ tác vụ nền hiện tại hoàn tất trước khi đóng app.",
            )
            return

        process = self.current_process
        if self.captured_task_running:
            if self._close_after_captured_run:
                self.status_var.set("Đang dừng tác vụ trước khi đóng")
                return
            if not messagebox.askyesno(
                "App đang chạy",
                "Buộc dừng tác vụ hiện tại và đóng app? Job đang chạy có thể phải thực hiện lại.",
            ):
                return
            self._close_after_captured_run = True
            self.status_var.set("Đang dừng tác vụ trước khi đóng")
            self.update_buttons()
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            # Keep Tk alive until the worker owns finalization. This also
            # covers the brief launch window before current_process is set.
            return

        try:
            config = load_config()
            if config.cleanup_on_exit:
                self.status_var.set("Đang dọn dẹp trước khi đóng")
                self.root.update_idletasks()
                cleanup_app_files(config, include_pip_cache=True)
        except Exception as exc:
            if not messagebox.askyesno(
                "Không dọn dẹp được",
                f"Không thể hoàn tất dọn dẹp tự động:\n\n{exc}\n\nVẫn đóng app?",
            ):
                self._install_after_close = None
                return
        installer = getattr(self, "_install_after_close", None)
        self._install_after_close = None
        if installer is not None:
            try:
                update_manager.launch_updater(
                    installer, ROOT, wait_pid=os.getpid(), relaunch=True,
                )
            except Exception as exc:
                self.write_output(f"[Cập nhật] Chưa bắt đầu cài đặt: {exc}\n")
                self.update_status_var.set("Chưa thể mở bộ cài. Ứng dụng vẫn đang mở.")
                self.update_detail_var.set(str(exc))
                return
        self._stop_update_worker()
        self.root.destroy()


    def run_captured(
        self, command: list[str], label: str, *, track_jobs: bool = False
    ) -> None:
        if self.is_running():
            messagebox.showwarning("Đang chạy", "App đang chạy một tác vụ khác.")
            return

        self._captured_run_counter += 1
        run_token = self._captured_run_counter
        self._active_captured_run = run_token
        self.captured_task_running = True
        self._force_stop_requested = False
        self._close_after_captured_run = False
        started_at = time.monotonic()
        self.status_var.set(label)
        self.write_output(f"\n--- {label} ---\n")
        self.update_buttons()

        def worker() -> None:
            code = -1
            summary: RunSummary | None = None
            try:
                if self._close_after_captured_run:
                    code = -2
                    return
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creation_flags |= subprocess.CREATE_NO_WINDOW

                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation_flags,
                )
                self.current_process = process
                if self._close_after_captured_run:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                self.root.after(0, self.update_buttons)
                if track_jobs:
                    self.root.after(0, self._start_job_progress_polling)
                assert process.stdout is not None
                for line in process.stdout:
                    parsed_summary = parse_run_summary(line)
                    if parsed_summary is not None:
                        summary = parsed_summary
                        continue
                    self.root.after(0, self.write_output, line)
                code = process.wait()
                self.root.after(0, self.write_output, f"Exit code: {code}\n")
            except Exception as exc:
                self.root.after(0, self.write_output, f"{label} failed: {exc}\n")
            finally:
                elapsed_seconds = time.monotonic() - started_at
                try:
                    self.root.after(
                        0,
                        self._finish_captured_run,
                        run_token,
                        label,
                        code,
                        track_jobs,
                        summary,
                        elapsed_seconds,
                    )
                except tk.TclError:
                    pass

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self.current_process = None
            self.captured_task_running = False
            self._active_captured_run = None
            self._force_stop_requested = False
            self._close_after_captured_run = False
            self.status_var.set("Không khởi động được tác vụ")
            self.update_buttons()
            self.write_output(f"{label} failed before launch: {exc}\n")
            messagebox.showerror("Không khởi động được tác vụ", str(exc))


    def _finish_captured_run(
        self,
        run_token: int,
        label: str,
        exit_code: int,
        track_jobs: bool,
        summary: RunSummary | None,
        elapsed_seconds: float,
    ) -> None:
        if run_token != self._active_captured_run:
            return

        forced = self._force_stop_requested
        close_after_run = self._close_after_captured_run
        self.current_process = None
        self.captured_task_running = False
        self._active_captured_run = None
        self._force_stop_requested = False
        self._close_after_captured_run = False
        if self.status_poll_after_id is not None:
            try:
                self.root.after_cancel(self.status_poll_after_id)
            except tk.TclError:
                pass
            self.status_poll_after_id = None

        if close_after_run:
            self.update_buttons()
            self.on_close()
            return

        self.refresh_status()
        if forced:
            self.status_var.set("Đã buộc dừng")
        elif exit_code != 0 or (summary is not None and summary.failed):
            self.status_var.set("Cần kiểm tra log")
        elif summary is not None and summary.stopped:
            self.status_var.set("Đã dừng theo yêu cầu")
        else:
            self.status_var.set("Sẵn sàng")
        self.update_buttons()

        if track_jobs:
            self._notify_task_completion(
                label,
                exit_code,
                elapsed_seconds,
                summary,
                forced=forced,
            )


    def _notify_task_completion(
        self,
        label: str,
        exit_code: int,
        elapsed_seconds: float,
        summary: RunSummary | None,
        *,
        forced: bool,
    ) -> None:
        notice = build_completion_notice(
            label,
            exit_code,
            elapsed_seconds,
            summary,
            forced=forced,
        )
        if notice is None:
            return
        try:
            config = load_config()
        except Exception:
            return

        if config.completion_sound_enabled:
            play_system_notification(notice.level)
        if config.completion_notification_enabled:
            self._show_completion_popup(notice)


    def _show_completion_popup(self, notice: CompletionNotice) -> None:
        self._close_completion_popup()
        popup: tk.Toplevel | None = None
        try:
            popup = tk.Toplevel(self.root)
            self.completion_popup = popup
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)

            accent = {
                "success": self.theme.success,
                "warning": self.theme.warning,
                "error": self.theme.danger,
            }[notice.level]
            popup.configure(background=accent)
            card = tk.Frame(
                popup,
                background=self.theme.surface,
                padx=16,
                pady=13,
                highlightthickness=0,
                borderwidth=0,
            )
            card.pack(fill="both", expand=True, padx=2, pady=2)
            title = tk.Label(
                card,
                text=notice.title,
                background=self.theme.surface,
                foreground=accent,
                font=("Segoe UI Semibold", 11),
                anchor="w",
            )
            title.pack(fill="x", padx=(0, 28))
            message = tk.Label(
                card,
                text=notice.message,
                background=self.theme.surface,
                foreground=self.theme.text,
                font=("Segoe UI", 9),
                justify="left",
                anchor="w",
                wraplength=340,
            )
            message.pack(fill="x", pady=(7, 0))
            close_label = tk.Label(
                card,
                text="×",
                background=self.theme.surface,
                foreground=self.theme.muted,
                font=("Segoe UI", 12),
                cursor="hand2",
            )
            close_label.place(relx=1.0, x=-2, y=-3, anchor="ne")
            close_label.bind(
                "<Button-1>",
                lambda _event, target=popup: self._close_completion_popup(target),
            )

            popup.update_idletasks()
            width = max(360, min(440, popup.winfo_reqwidth()))
            height = popup.winfo_reqheight()
            screen_width = popup.winfo_screenwidth()
            screen_height = popup.winfo_screenheight()
            x = max(12, screen_width - width - 24)
            y = max(12, screen_height - height - 72)
            popup.geometry(f"{width}x{height}+{x}+{y}")
            popup.lift()
            popup.bind(
                "<Escape>",
                lambda _event, target=popup: self._close_completion_popup(target),
            )
            popup.after(
                8000,
                lambda target=popup: self._close_completion_popup(target),
            )
        except tk.TclError:
            if popup is not None:
                try:
                    popup.destroy()
                except tk.TclError:
                    pass
            self.completion_popup = None


    def _close_completion_popup(self, popup: tk.Toplevel | None = None) -> None:
        target = popup or self.completion_popup
        if target is None:
            return
        try:
            if target.winfo_exists():
                target.destroy()
        except tk.TclError:
            pass
        if self.completion_popup is target:
            self.completion_popup = None


    def write_config_file(self, path: Path, text: str) -> None:
        attrs = getattr(path.stat(), "st_file_attributes", 0) if path.exists() else 0
        was_hidden = bool(attrs & 0x2)
        was_readonly = bool(attrs & 0x1)

        if os.name == "nt" and path.exists():
            self.set_windows_file_attributes(path, "-h", "-r")

        try:
            path.write_text(text, encoding="utf-8")
        finally:
            if os.name == "nt" and path.exists():
                if was_readonly:
                    self.set_windows_file_attributes(path, "+r")
                if was_hidden:
                    self.set_windows_file_attributes(path, "+h")


    def set_windows_file_attributes(self, path: Path, *flags: str) -> None:
        creation_flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags = subprocess.CREATE_NO_WINDOW
        subprocess.run(
            ["attrib", *flags, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            check=False,
        )


    def is_running(self) -> bool:
        return (
            getattr(self, "_closing", False)
            or self.maintenance_running
            or self.captured_task_running
            or (
                self.current_process is not None
                and self.current_process.poll() is None
            )
        )


    def _count_images(
        self, folder: Path, extensions: tuple[str, ...] = SUPPORTED_IMAGE_EXTS
    ) -> int:
        if not folder.exists():
            return 0
        return sum(
            1
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in extensions
        )



def main() -> int:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    PngMockupGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

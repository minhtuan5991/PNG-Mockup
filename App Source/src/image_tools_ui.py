"""Inline file gallery and progressively disclosed image-tool controls."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from image_tools import (
    ToolImage, ToolOptions, inspect_tool_image, run_tool_batch, validate_tool_inputs,
)
from output_watermark import inspect_watermark
from transparent_image_layout import LAYOUT_SPECS, SMALL_IMAGE_LONG_EDGE_PX
from ui_widgets import ScrollPanel
from windows_file_drop import WindowsFileDrop


class ImageToolsWorkspace(ttk.Frame):
    def __init__(self, parent, app) -> None:
        super().__init__(parent, style="App.TFrame")
        self.app = app
        self.images: dict[Path, ToolImage] = {}
        self.selected: set[Path] = set()
        self.output_dir: Path | None = None
        self.loading = False
        self._pending_imports: list[Path] = []
        self._alive = True
        self._events: queue.Queue = queue.Queue()
        self._thumb_requests: queue.Queue = queue.Queue()
        self._thumb_pending: set[Path] = set()
        self._thumb_cache: OrderedDict[Path, ImageTk.PhotoImage | None] = OrderedDict()
        self._thumb_thread = None
        self._draw_after = None
        self._hit_boxes: list[tuple[Path, tuple[int, int, int, int]]] = []
        self._cursor_index = 0
        self.controls: list[ttk.Widget] = []
        self.features = {name: tk.BooleanVar(self, value=False) for name in ("resize", "compress", "metadata", "watermark")}
        self.layout_var = tk.StringVar(self, value="4500x5400")
        self.watermark_var = tk.StringVar(self, value="")
        self.search_var = tk.StringVar(self, value="")
        self.count_var = tk.StringVar(self, value="Đã chọn 0/0 ảnh")
        self.feedback_var = tk.StringVar(self, value="Chọn tệp hoặc kéo thả ảnh từ Explorer vào vùng bên dưới.")
        self.progress_text = tk.StringVar(self, value="Chưa bắt đầu")
        self.output_var = tk.StringVar(self, value="Kết quả: thư mục mới bên cạnh tệp đầu tiên trong danh sách xử lý.")
        self.progress_var = tk.DoubleVar(self, value=0)
        self._build()
        self.drop_target = WindowsFileDrop(self.gallery, self.add_files)
        self.search_var.trace_add("write", lambda *_: self._queue_draw())
        self.bind("<Destroy>", self._destroyed, add="+")
        self._poll_after = self.after(50, self._poll)
        self.update_state()

    def _label(self, parent, text=None, **kwargs):
        label = ttk.Label(parent, text=text, style="SectionMuted.TLabel", justify="left", **kwargs)
        label.pack(fill="x", pady=(4, 0))
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(240, e.width)))
        return label

    def _build(self) -> None:
        # Pack the footer first so action buttons are always outside page scroll.
        footer = ttk.Frame(self, padding=(0, 12, 16, 0), style="App.TFrame")
        footer.pack(side="bottom", fill="x")
        actions = ttk.Frame(footer, style="App.TFrame")
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        self.start_button = self.app._button(actions, "Bắt đầu xử lý", self.start, "image_tools_start", primary=True, icon="play")
        self.start_button.pack(side="right")
        self.open_button = self.app._button(actions, "Mở thư mục kết quả", self.open_output, "image_tools_output", icon="folder", lock=False)
        self.open_button.pack(side="right", padx=(0, 8))
        progress_label = ttk.Label(footer, textvariable=self.progress_text, style="SectionMuted.TLabel")
        progress_label.pack(fill="x", pady=(4, 0))
        ttk.Progressbar(footer, variable=self.progress_var, maximum=100).pack(fill="x")
        scroll = ScrollPanel(self, background=self.app.theme.bg)
        scroll.pack(side="top", fill="both", expand=True)
        self.app.scroll_panels.append(scroll)
        self.scroll = scroll
        body = scroll.body
        source = self.app._section(body, padding=18)
        source.pack(fill="x", pady=(0, 14))
        head = ttk.Frame(source.body, style="Section.TFrame")
        head.pack(fill="x")
        title = ttk.Label(head, text="01 / Chọn ảnh để xử lý", style="CardTitle.TLabel")
        choose = self.app._button(head, "Chọn tệp ảnh", self.choose_files, "image_tools_choose", icon="folder")
        choose.pack(side="right", padx=(12, 0))
        title.configure(wraplength=260, justify="left")
        title.pack(side="left", fill="x", expand=True)
        title.bind("<Configure>", lambda e: title.configure(wraplength=max(160, e.width)))
        self.controls.append(choose)
        self._label(source.body, "Thêm trực tiếp PNG, JPG, JPEG hoặc WebP. Có thể chọn tệp từ nhiều thư mục.")
        toolbar = ttk.Frame(source.body, style="Section.TFrame")
        toolbar.pack(fill="x", pady=(12, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Tìm theo tên", style="Section.TLabel").grid(row=0, column=0, padx=(0, 8))
        search = ttk.Entry(toolbar, textvariable=self.search_var, width=12)
        search.grid(row=0, column=1, sticky="ew")
        for column, (text, command) in enumerate((("Chọn tất cả", lambda: self.select_all(True)), ("Bỏ chọn", lambda: self.select_all(False))), 2):
            button = ttk.Button(toolbar, text=text, command=command)
            button.grid(row=0, column=column, padx=(8, 0))
            self.controls.append(button)
        gallery_shell = ttk.Frame(source.body, style="Section.TFrame")
        gallery_shell.pack(fill="x")
        scale = max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72))
        self._scale = scale
        self._thumbnail_size = (round(92 * scale), round(72 * scale))
        self._tile_width, self._tile_height = round(146 * scale), round(138 * scale)
        self.gallery = tk.Canvas(gallery_shell, height=round(185 * scale), highlightthickness=1, takefocus=True)
        self.gallery.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(gallery_shell, orient="vertical", command=self._scroll_gallery)
        bar.pack(side="right", fill="y", padx=(4, 0))
        self.gallery.configure(yscrollcommand=lambda a, b: (bar.set(a, b), self._queue_draw()))
        self.gallery.bind("<Configure>", lambda _e: self._queue_draw())
        self.gallery.bind("<Button-1>", self._click_gallery)
        self.gallery.bind("<Return>", self._keyboard_toggle)
        self.gallery.bind("<space>", self._keyboard_toggle)
        for key in ("Left", "Right", "Up", "Down", "Home", "End"):
            self.gallery.bind(f"<{key}>", self._keyboard_move)
        self.gallery.bind("<Control-a>", lambda _e: (self.select_all(True), "break")[-1])
        self.gallery.bind("<Delete>", lambda _e: self.remove_selected())
        self.gallery.bind("<MouseWheel>", self._wheel_gallery)
        self.gallery.bind("<FocusIn>", lambda _e: self.gallery.configure(highlightbackground=self.app.theme.accent))
        self.gallery.bind("<FocusOut>", lambda _e: self.apply_theme())
        bottom = ttk.Frame(source.body, style="Section.TFrame")
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, textvariable=self.count_var, style="Section.TLabel").pack(side="left")
        remove = ttk.Button(bottom, text="Loại khỏi danh sách", command=self.remove_selected)
        remove.pack(side="right")
        self.controls.append(remove)
        self.feedback_label = self._label(source.body, textvariable=self.feedback_var)

        features = self.app._section(body, "02 / Chọn tính năng", padding=18)
        features.pack(fill="x", pady=(0, 14))
        switches = ttk.Frame(features.body, style="Section.TFrame")
        switches.pack(fill="x")
        self.feature_checks = {}
        for index, (key, label) in enumerate((("resize", "Kích thước"), ("compress", "Nén PNG"), ("metadata", "Metadata"), ("watermark", "Watermark"))):
            switches.columnconfigure(index, weight=1)
            check = ttk.Checkbutton(switches, text=label, variable=self.features[key], command=self._options_changed)
            check.grid(row=0, column=index, sticky="w", padx=(0, 8))
            self.feature_checks[key] = check
            self.controls.append(check)
        switches.bind("<Configure>", lambda e: self._layout_features(switches, e.width))
        self._label(features.body, "Bật một hoặc nhiều tính năng. Tùy chọn tương ứng sẽ xuất hiện bên dưới.")
        self.options_host = ttk.Frame(features.body, style="Section.TFrame")
        self.options_host.pack(fill="x")
        self.option_panels = {}
        for key, label in (("resize", "Kích thước"), ("compress", "Nén PNG"), ("metadata", "Metadata"), ("watermark", "Watermark")):
            self.option_panels[key] = ttk.LabelFrame(self.options_host, text=label, padding=12)
        resize = self.option_panels["resize"]
        sizes = ttk.Frame(resize, style="Section.TFrame")
        sizes.pack(fill="x")
        size_choices: list[ttk.Radiobutton] = []
        for column, (value, (width, height, _, _)) in enumerate(LAYOUT_SPECS.items()):
            radio = ttk.Radiobutton(
                sizes, text=f"{width} × {height} px", variable=self.layout_var, value=value,
            )
            radio.grid(row=0, column=column, sticky="w", padx=(0, 24))
            size_choices.append(radio)
            self.controls.append(radio)

        def fit_sizes(event: tk.Event) -> None:
            column_width = max(radio.winfo_reqwidth() for radio in size_choices) + 24
            columns = max(1, min(len(size_choices), event.width // column_width))
            for index, radio in enumerate(size_choices):
                radio.grid_configure(row=index // columns, column=index % columns)

        sizes.bind("<Configure>", fit_sizes)
        self._label(
            resize,
            "PNG nền trong suốt · Căn giữa theo chiều ngang · Lanczos · 300 DPI.\n"
            "Khổ 2000 × 2000 px sát mép trên. Hai khổ dọc cách mép trên 25 px.",
        )
        self._label(self.option_panels["compress"], "Một chế độ nén tự động cho POD: giữ kích thước, ICC và DPI; kiểm tra chất lượng màu/alpha trước khi lưu. Mục tiêu giảm ≥35%; ảnh đã tối ưu có thể giảm ít hơn.")
        self._label(self.option_panels["metadata"], "Xóa Comment, EXIF, XMP, thumbnail EXIF, IPTC và ICC. Giữ nguyên pixel và DPI; không mã hóa lại ảnh. Khi bật Watermark, Metadata luôn được bật như trước.")
        watermark = self.option_panels["watermark"]
        row = ttk.Frame(watermark, style="Section.TFrame")
        row.pack(fill="x")
        entry = ttk.Entry(row, textvariable=self.watermark_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        choose_watermark = ttk.Button(row, text="Chọn PNG Watermark", command=self.choose_watermark)
        choose_watermark.pack(side="right")
        self.controls.append(choose_watermark)
        self._label(watermark, "PNG trong suốt 2000×2000 px / 72 DPI. Ảnh kết quả phải có cùng kích thước; xóa metadata sau khi gắn.")
        result = self.app._section(body, "03 / Kết quả", padding=18)
        result.pack(fill="x", pady=(0, 14))
        self._label(result.body, textvariable=self.output_var)
        self._label(result.body, "Mỗi lần chạy tạo một thư mục mới. Mỗi ảnh có một bản kết quả sau toàn bộ tác vụ đã chọn; ảnh gốc luôn được giữ nguyên.")
        self.maintenance_host = body
        self.apply_theme()

    def choose_files(self) -> None:
        if self.app.is_running():
            return
        names = filedialog.askopenfilenames(
            parent=self.app.root, title="Chọn ảnh để xử lý",
            filetypes=[("Ảnh hỗ trợ", "*.png *.jpg *.jpeg *.webp"), ("PNG", "*.png"), ("Tất cả tệp", "*.*")],
        )
        if names:
            self.add_files([Path(name) for name in names])

    def _layout_features(self, parent, width: int) -> None:
        checks = list(self.feature_checks.values())
        columns = 4 if sum(check.winfo_reqwidth() + 12 for check in checks) <= width else 2
        for column in range(4):
            parent.columnconfigure(column, weight=1 if column < columns else 0)
        for index, check in enumerate(checks):
            check.grid_configure(row=index // columns, column=index % columns)

    def add_files(self, paths) -> None:
        if self.app.is_running():
            self.feedback_var.set("Chờ tác vụ hiện tại hoàn tất trước khi thêm ảnh.")
            return
        incoming = [Path(path) for path in paths]
        if self.loading:
            self._pending_imports.extend(incoming)
            return
        self.loading = True
        self.feedback_var.set("Đang đọc danh sách tệp…")
        self.update_state()
        known = set(self.images)

        def worker():
            errors = []
            for raw in incoming:
                if not self._alive:
                    return
                try:
                    path = raw.resolve()
                    if path in known:
                        continue
                    image = inspect_tool_image(path)
                    known.add(path)
                    self._events.put(("image", image))
                except Exception as exc:
                    errors.append(f"{raw.name}: {exc}")
            self._events.put(("import_done", errors))

        try:
            threading.Thread(target=worker, daemon=True).start()
        except (RuntimeError, OSError) as exc:
            self.loading = False
            self.feedback_var.set(str(exc))
            self.update_state()

    def _options_changed(self) -> None:
        watermark = self.features["watermark"].get()
        if watermark:
            self.features["metadata"].set(True)
        for panel in self.option_panels.values():
            panel.pack_forget()
        for key, variable in self.features.items():
            if variable.get():
                self.option_panels[key].pack(fill="x", pady=(12, 0))
        self.update_state()

    def choose_watermark(self) -> None:
        if self.app.is_running():
            return
        name = filedialog.askopenfilename(parent=self.app.root, title="Chọn PNG Watermark", filetypes=[("PNG", "*.png")])
        if not name:
            return
        try:
            info = inspect_watermark(name)
        except Exception as exc:
            messagebox.showerror("Watermark không hợp lệ", str(exc), parent=self.app.root)
            return
        self.watermark_var.set(str(info.path))

    def current_options(self) -> ToolOptions:
        if self.features["watermark"].get() and not self.watermark_var.get():
            raise ValueError("Hãy chọn tệp PNG Watermark.")
        return ToolOptions(
            layout=self.layout_var.get() if self.features["resize"].get() else None,
            compress=self.features["compress"].get(),
            metadata=self.features["metadata"].get() or self.features["watermark"].get(),
            watermark=Path(self.watermark_var.get()) if self.features["watermark"].get() else None,
        )

    def select_all(self, value: bool) -> None:
        if self.app.is_running() or self.loading:
            return
        visible = self._filtered_paths()
        if value:
            self.selected.update(visible)
        else:
            self.selected.difference_update(visible)
        self._selection_changed()

    def remove_selected(self) -> None:
        if self.app.is_running() or self.loading:
            return
        for path in self.selected:
            self.images.pop(path, None)
            self._thumb_cache.pop(path, None)
        self.selected.clear()
        self._selection_changed()

    def _selection_changed(self) -> None:
        self.count_var.set(f"Đã chọn {len(self.selected)}/{len(self.images)} ảnh")
        self.update_state()
        self._queue_draw()

    def update_state(self) -> None:
        busy = self.app.is_running()
        for widget in self.controls:
            widget.configure(state="disabled" if busy else "normal")
        self.feature_checks["metadata"].configure(state="disabled" if busy or self.features["watermark"].get() else "normal")
        ready = not busy and not self.loading and bool(self.selected) and any(v.get() for v in self.features.values())
        self.start_button.configure(state="normal" if ready else "disabled")
        self.open_button.configure(state="normal" if self.output_dir else "disabled")

    def start(self) -> None:
        if self.app.is_running() or self.loading:
            return
        selected = [image for path, image in self.images.items() if path in self.selected]
        try:
            options = self.current_options()
            validate_tool_inputs(selected, options)
        except Exception as exc:
            self.feedback_var.set(str(exc))
            return
        small = [item for item in selected if max(item.width, item.height) < SMALL_IMAGE_LONG_EDGE_PX]
        if options.layout and small:
            if not messagebox.askyesno("Ảnh có kích thước nhỏ", f"Có {len(small)} ảnh có cạnh dài nhất nhỏ hơn {SMALL_IMAGE_LONG_EDGE_PX} px. Tiếp tục thay đổi kích thước?", parent=self.app.root):
                return
        self.app.maintenance_running = True
        self.app.status_var.set("Đang xử lý ảnh")
        self.progress_var.set(0)
        self.progress_text.set(f"Đang bắt đầu · 0/{len(selected)} ảnh")
        self.feedback_var.set("Đang xử lý lần lượt các ảnh đã chọn…")
        self.app.update_buttons()

        def worker():
            try:
                result = run_tool_batch(
                    [item.path for item in selected], options,
                    progress=lambda done, total, status: self._events.put(("progress", (done, total, status))),
                )
                self._events.put(("done", result))
            except Exception as exc:
                self._events.put(("failed", str(exc)))

        try:
            threading.Thread(target=worker, daemon=True).start()
        except (RuntimeError, OSError) as exc:
            self._finish_error(str(exc))

    def _finish_error(self, message: str) -> None:
        self.app.maintenance_running = False
        self.app.status_var.set("Sẵn sàng")
        self.progress_text.set("Chưa hoàn tất")
        self.feedback_var.set(message)
        self.app.write_output(f"\n[Công cụ ảnh] {message}\n")
        self.app.update_buttons()

    def open_output(self) -> None:
        if self.output_dir:
            self.app.open_path(self.output_dir)

    def _poll(self) -> None:
        self._poll_after = None
        for _ in range(80):
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "image":
                self.images[payload.path] = payload
                self.selected.add(payload.path)
            elif kind == "import_done":
                self.loading = False
                self.feedback_var.set("Chọn/bỏ chọn bằng chuột hoặc phím mũi tên + Space. Bật tính năng bên dưới để tiếp tục." if not payload else "Một số tệp bị bỏ qua: " + "; ".join(payload[:3]))
                self._selection_changed()
                if self._pending_imports:
                    pending, self._pending_imports = self._pending_imports, []
                    self.add_files(pending)
            elif kind == "thumbnail":
                path, data = payload
                self._thumb_pending.discard(path)
                if path in self.images:
                    photo = None
                    if data:
                        width, height, pixels = data
                        with Image.frombytes("RGBA", (width, height), pixels) as image:
                            photo = ImageTk.PhotoImage(image, master=self)
                    self._thumb_cache[path] = photo
                    self._thumb_cache.move_to_end(path)
                    while len(self._thumb_cache) > 200:
                        self._thumb_cache.popitem(last=False)
                    self._queue_draw()
            elif kind == "progress":
                done, total, status = payload
                self.progress_var.set(done * 100 / max(total, 1))
                self.progress_text.set(f"{done}/{total} ảnh")
                self.feedback_var.set(status)
            elif kind == "failed":
                self._finish_error(payload)
            elif kind == "done":
                self.app.maintenance_running = False
                self.app.status_var.set("Sẵn sàng")
                self.output_dir = payload.output_dir
                self.output_var.set(f"Kết quả: {payload.output_dir}")
                summary = f"Hoàn tất: {len(payload.outputs)} ảnh · {len(payload.failures)} lỗi"
                compressed = [item.compression for item in payload.outputs if item.compression]
                if compressed:
                    total = sum(item.input_bytes_for_compression for item in compressed)
                    saved = sum(item.saved_bytes for item in compressed)
                    target_count = sum(item.target_achieved for item in compressed)
                    fallback_count = sum(item.compression_mode.startswith("lossless") for item in compressed)
                    summary += (
                        f" · Nén giảm {saved / max(total, 1):.1%} ({saved / 1024**2:.2f} MB)"
                        f" · Đạt ≥35%: {target_count}/{len(compressed)}"
                        f" · Fallback lossless: {fallback_count}"
                    )
                self.progress_text.set(f"{len(payload.outputs)} xong · {len(payload.failures)} lỗi")
                self.progress_var.set(100)
                errors = "\n".join(f"- {path.name}: {detail}" for path, detail in payload.failures)
                self.feedback_var.set(summary + (". " + errors[:700] if errors else "."))
                self.app.write_output(f"\n--- Công cụ ảnh ---\n{summary}\nKết quả: {payload.output_dir}\n{errors}\n")
                self.app.update_buttons()
        if self._alive:
            self._poll_after = self.after(50, self._poll)

    def _filtered_paths(self) -> list[Path]:
        term = self.search_var.get().strip().casefold()
        return [path for path in self.images if term in path.name.casefold()]

    def _queue_draw(self) -> None:
        if self._alive and self._draw_after is None:
            self._draw_after = self.after_idle(self._draw_gallery)

    def _draw_gallery(self) -> None:
        self._draw_after = None
        c = self.gallery
        t = self.app.theme
        width, height = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        paths = self._filtered_paths()
        columns = max(1, (width - 12) // self._tile_width)
        rows = (len(paths) + columns - 1) // columns
        region = (0, 0, width - 2, max(height - 2, rows * self._tile_height + 12))
        if c.cget("scrollregion") != " ".join(str(value) for value in region):
            c.configure(scrollregion=region)
        c.delete("all")
        self._hit_boxes.clear()
        if not paths:
            label = "Không có ảnh khớp tên tìm kiếm." if self.images else "Kéo thả tệp ảnh vào đây"
            c.create_text(width // 2, height // 2 - 12, text=label, font=("Segoe UI", 13, "bold"), fill=t.text, width=max(200, width - 40))
            c.create_text(width // 2, height // 2 + 20, text="hoặc bấm Chọn tệp ảnh / nhấn Enter", font=("Segoe UI", 10), fill=t.muted)
            return
        top = max(0, int(c.canvasy(0)) // self._tile_height)
        bottom = min(rows, top + height // self._tile_height + 2)
        for index in range(top * columns, min(len(paths), bottom * columns)):
            path = paths[index]
            x = 6 + index % columns * self._tile_width
            y = 6 + index // columns * self._tile_height
            x2, y2 = x + self._tile_width - 8, y + self._tile_height - 8
            selected = path in self.selected
            c.create_rectangle(x, y, x2, y2, fill=t.selected if selected else t.surface, outline=t.accent if selected else t.border, width=2 if selected else 1)
            if index == self._cursor_index and self.gallery.focus_get() is self.gallery:
                c.create_rectangle(x + 3, y + 3, x2 - 3, y2 - 3, outline=t.accent, dash=(3, 2))
            c.create_rectangle(x + 8, y + 8, x + 22, y + 22, fill=t.accent if selected else t.surface_alt, outline=t.accent)
            if selected:
                c.create_line(x + 11, y + 15, x + 15, y + 19, x + 20, y + 11, fill=t.accent_text, width=2)
            center = (x + x2) // 2
            if path in self._thumb_cache:
                photo = self._thumb_cache[path]
                self._thumb_cache.move_to_end(path)
                if photo:
                    c.create_image(center, y + round(52 * self._scale), image=photo)
                else:
                    c.create_text(center, y + round(52 * self._scale), text=path.suffix[1:].upper(), fill=t.muted)
            elif path not in self._thumb_pending:
                self._request_thumbnail(path)
            label = path.name if len(path.name) < 24 else path.name[:20] + "…"
            c.create_text(center, y2 - round(31 * self._scale), text=label, font=("Segoe UI", 9), fill=t.text, width=self._tile_width - 16)
            info = self.images[path]
            c.create_text(center, y2 - round(12 * self._scale), text=f"{info.width}×{info.height}", font=("Segoe UI", 8), fill=t.muted)
            self._hit_boxes.append((path, (x, y, x2, y2)))

    def _request_thumbnail(self, path: Path) -> None:
        if self._thumb_thread is None:
            self._thumb_thread = threading.Thread(target=self._thumbnail_worker, daemon=True)
            try:
                self._thumb_thread.start()
            except (RuntimeError, OSError):
                self._thumb_thread = None
                self._thumb_cache[path] = None
                return
        self._thumb_pending.add(path)
        self._thumb_requests.put(path)

    def _thumbnail_worker(self) -> None:
        while self._alive:
            path = self._thumb_requests.get()
            if path is None:
                return
            data = None
            try:
                with Image.open(path) as source:
                    source.thumbnail(self._thumbnail_size, Image.Resampling.LANCZOS)
                    with source.convert("RGBA") as image:
                        data = (image.width, image.height, image.tobytes())
            except Exception:
                pass
            self._events.put(("thumbnail", (path, data)))

    def _click_gallery(self, event) -> None:
        self.gallery.focus_set()
        if self.app.is_running() or self.loading:
            return
        x, y = self.gallery.canvasx(event.x), self.gallery.canvasy(event.y)
        for path, (x1, y1, x2, y2) in self._hit_boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                if path in self.selected:
                    self.selected.remove(path)
                else:
                    self.selected.add(path)
                self.feedback_var.set(str(path))
                self._cursor_index = self._filtered_paths().index(path)
                self._selection_changed()
                break

    def _keyboard_toggle(self, _event) -> str:
        if self.app.is_running() or self.loading:
            return "break"
        paths = self._filtered_paths()
        if not paths:
            self.choose_files()
            return "break"
        self._cursor_index = min(self._cursor_index, len(paths) - 1)
        path = paths[self._cursor_index]
        if path in self.selected:
            self.selected.remove(path)
        else:
            self.selected.add(path)
        self.feedback_var.set(str(path))
        self._selection_changed()
        return "break"

    def _keyboard_move(self, event) -> str:
        paths = self._filtered_paths()
        if not paths:
            return "break"
        columns = max(1, (self.gallery.winfo_width() - 12) // self._tile_width)
        offsets = {"Left": -1, "Right": 1, "Up": -columns, "Down": columns}
        if event.keysym == "Home":
            self._cursor_index = 0
        elif event.keysym == "End":
            self._cursor_index = len(paths) - 1
        else:
            self._cursor_index = max(0, min(len(paths) - 1, self._cursor_index + offsets[event.keysym]))
        y = (self._cursor_index // columns) * self._tile_height
        top = self.gallery.canvasy(0)
        height = self.gallery.winfo_height()
        total = max(height, ((len(paths) + columns - 1) // columns) * self._tile_height + 12)
        if y < top:
            self.gallery.yview_moveto(y / total)
        elif y + self._tile_height > top + height:
            self.gallery.yview_moveto((y + self._tile_height - height + 12) / total)
        self.feedback_var.set(str(paths[self._cursor_index]))
        self._queue_draw()
        return "break"

    def _scroll_gallery(self, *args) -> None:
        self.gallery.yview(*args)
        self._queue_draw()

    def _wheel_gallery(self, event) -> str:
        self.gallery.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self._queue_draw()
        return "break"

    def apply_theme(self) -> None:
        t = self.app.theme
        self.gallery.configure(background=t.surface_alt, highlightbackground=t.border, highlightcolor=t.accent)
        self._queue_draw()

    def _destroyed(self, event) -> None:
        if event.widget is not self:
            return
        self._alive = False
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
        if self._draw_after is not None:
            self.after_cancel(self._draw_after)
        self.drop_target.close()
        self._thumb_requests.put(None)

"""Native ttk controls and original code-drawn line icons; no raster skins."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable
from PIL import Image, ImageDraw, ImageTk

def make_icon(master: tk.Misc, name: str, color: str, size: int = 20) -> ImageTk.PhotoImage:
    """Draw a consistent 24-unit outline symbol with antialiased edges."""
    scale = max(3, round(size / 6))
    image = Image.new("RGBA", (24 * scale, 24 * scale))
    draw = ImageDraw.Draw(image)
    width = max(2, round(1.7 * scale))
    def line(points: list[tuple[float, float]]) -> None:
        draw.line([(round(x * scale), round(y * scale)) for x, y in points], fill=color, width=width, joint="curve")
    def rect(box: tuple[float, float, float, float], radius: float = 2) -> None:
        draw.rounded_rectangle(tuple(round(n * scale) for n in box), radius=round(radius * scale), outline=color, width=width)
    def ellipse(box: tuple[float, float, float, float]) -> None:
        draw.ellipse(tuple(round(n * scale) for n in box), outline=color, width=width)
    if name in {"mockup", "layers", "brand"}:
        line([(3, 8), (12, 3), (21, 8), (12, 13), (3, 8)])
        line([(3, 12), (12, 17), (21, 12)])
        line([(3, 16), (12, 21), (21, 16)])
    elif name in {"image", "png"}:
        rect((3, 3, 21, 21)); ellipse((6, 6, 10, 10))
        line([(4, 18), (10, 12), (14, 16), (17, 12), (21, 17)])
    elif name in {"tools", "resize"}:
        line([(3, 9), (3, 3), (9, 3)]); line([(3, 3), (10, 10)])
        line([(15, 21), (21, 21), (21, 15)]); line([(14, 14), (21, 21)])
        line([(15, 3), (21, 3), (21, 9)]); line([(3, 15), (3, 21), (9, 21)])
    elif name == "settings":
        for x, knob in ((6, 8), (12, 16), (18, 7)):
            line([(x, 3), (x, knob - 2)]); line([(x, knob + 2), (x, 21)])
            ellipse((x - 2, knob - 2, x + 2, knob + 2))
    elif name in {"folder", "output", "logs"}:
        line([(3, 8), (3, 5), (10, 5), (12, 8), (21, 8), (21, 20), (3, 20), (3, 8)])
        line([(3, 10), (21, 10)])
    elif name in {"play", "run"}:
        line([(8, 4), (20, 12), (8, 20), (8, 4)])
    elif name == "one_job":
        line([(6, 5), (16, 12), (6, 19), (6, 5)]); line([(20, 5), (20, 19)])
    elif name == "stop":
        rect((6, 6, 18, 18), 1)
    elif name in {"rename", "edit"}:
        line([(4, 16), (16, 4), (20, 8), (8, 20), (4, 20), (4, 16)])
        line([(13, 7), (17, 11)])
    elif name in {"watermark", "shield"}:
        line([(12, 3), (20, 6), (19, 15), (16, 19), (12, 21), (8, 19), (5, 15), (4, 6), (12, 3)])
        line([(8, 12), (11, 15), (16, 9)])
    elif name in {"refresh", "queue"}:
        draw.arc((4 * scale, 4 * scale, 20 * scale, 20 * scale), 45, 305, fill=color, width=width)
        line([(20, 3), (20, 9), (14, 9)])
    elif name == "check":
        line([(5, 12), (10, 17), (20, 6)])
    elif name == "diagnose":
        rect((3, 4, 21, 18), 1); line([(8, 22), (16, 22)])
        line([(12, 18), (12, 22)]); line([(7, 11), (10, 14), (17, 8)])
    elif name == "cleanup":
        line([(4, 7), (20, 7)]); line([(9, 7), (9, 3), (15, 3), (15, 7)])
        line([(6, 8), (7, 21), (17, 21), (18, 8)])
        line([(10, 10), (10, 17)]); line([(14, 10), (14, 17)])
    else:
        ellipse((3, 3, 21, 21)); line([(12, 7), (12, 13)])
        ellipse((11.6, 16.3, 12.4, 17.1))
    return ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=master)

class StudioButton(ttk.Button):
    """A real button: text is accessible; Tab/Space/Return all remain usable."""
    def __init__(self, master: tk.Misc, *, text: str, command: Callable[[], Any] | None,
                 theme: Any = None, icon: Any = None, primary: bool = False,
                 width: int | None = None, height: int | None = None,
                 backdrop: str | None = None, style: str | None = None) -> None:
        options: dict[str, Any] = {"text": text, "command": command,
            "style": style or ("Primary.TButton" if primary else "TButton"), "takefocus": True}
        if width is not None: options["width"] = width
        if icon is not None: options.update(image=icon, compound="left")
        super().__init__(master, **options)
        self.bind("<Return>", self._keyboard_invoke)
    def _keyboard_invoke(self, _event: tk.Event) -> str:
        if not self.instate(["disabled"]): self.invoke()
        return "break"

class StudioSection(ttk.Frame):
    def __init__(self, master: tk.Misc, *, title: str = "", theme: Any = None,
                 padding: int = 18, backdrop: str | None = None) -> None:
        super().__init__(master, padding=padding, style="Card.TFrame")
        if title:
            ttk.Label(self, text=title, style="SectionHeading.TLabel").pack(anchor="w", pady=(0, 12))
        self.body = ttk.Frame(self, style="Section.TFrame")
        self.body.pack(fill="both", expand=True)

class ScrollPanel(ttk.Frame):
    """A width-following page; the app routes the wheel only under the pointer."""
    def __init__(self, master: tk.Misc, *, background: str, stretch: bool = False) -> None:
        super().__init__(master, style="App.TFrame")
        self._stretch = stretch
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=background)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", padx=(8, 0))
        self.body = ttk.Frame(self.canvas, style="App.TFrame")
        self._window = self.canvas.create_window((0, 0), anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._layout)
        self.canvas.bind("<Configure>", self._layout)

    def _layout(self, _event: tk.Event | None = None) -> None:
        self.canvas.itemconfigure(self._window, width=self.canvas.winfo_width())
        if self._stretch:
            self.canvas.itemconfigure(
                self._window,
                height=max(self.canvas.winfo_height(), self.body.winfo_reqheight()),
            )
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def reveal_focus(self) -> None:
        widget = self.focus_get()
        if widget is None or not str(widget).startswith(str(self.body) + "."): return
        self.update_idletasks()
        y = widget.winfo_rooty() - self.body.winfo_rooty()
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        height = max(self.body.winfo_height(), 1)
        if y < top + 12: self.canvas.yview_moveto(max(0, y - 12) / height)
        elif y + widget.winfo_height() > bottom - 12:
            self.canvas.yview_moveto((y + widget.winfo_height() + 12 - self.canvas.winfo_height()) / height)

def render_theme_skin(*args: Any, **kwargs: Any) -> None:
    """Utility thumbnails deliberately use a plain, readable canvas."""
    return None

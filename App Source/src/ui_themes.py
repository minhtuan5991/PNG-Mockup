"""One PNG Mockup visual style, five semantic palettes, no external skins."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THEME_ROOT = PROJECT_ROOT / "Theme"
DEFAULT_THEME = "png_modern"
DEFAULT_COLOR_PRESET = "soft_purple"
COLOR_FIELDS = (
    "bg", "surface", "surface_alt", "sidebar", "header", "text", "muted",
    "accent", "accent_hover", "accent_text", "border", "selected", "success",
    "warning", "danger", "soft_green_border", "log_bg", "log_fg", "thumb_bg",
)

@dataclass(frozen=True)
class UITheme:
    key: str
    label: str
    description: str
    bg: str
    surface: str
    surface_alt: str
    sidebar: str
    header: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_text: str
    border: str
    selected: str
    success: str
    warning: str
    danger: str
    soft_green_border: str
    log_bg: str
    log_fg: str
    thumb_bg: str
    native: bool = False
    visual_style: str = "png_modern"
    color_preset: str = DEFAULT_COLOR_PRESET
    card_palette: tuple[str, ...] = ()

COLOR_PRESETS: dict[str, dict[str, Any]] = {
    "mint": {
        "label": "Mint", "description": "Trắng nhẹ và xanh bạc hà, điểm nhấn xanh ngọc.",
        "swatch": "#076E63",
        "colors": {
            "bg": "#F3F8F6", "surface": "#FFFFFF", "surface_alt": "#EDF5F1",
            "sidebar": "#E6F1EC", "header": "#F3F8F6", "text": "#18352F",
            "muted": "#536D65", "accent": "#076E63", "accent_hover": "#05594E",
            "accent_text": "#FFFFFF", "border": "#CFDFD7", "selected": "#D5EBE2",
            "success": "#187046", "warning": "#90600B", "danger": "#B23F4C",
            "soft_green_border": "#9BCFBD",
            "log_bg": "#FBFDFC", "log_fg": "#29443B", "thumb_bg": "#EDF5F1",
        },
    },
    "soft_purple": {
        "label": "Tím nhẹ", "description": "Nền sáng trung tính, sắc tím dịu và chi tiết rõ ràng.",
        "swatch": "#6548B9",
        "colors": {
            "bg": "#F5F4F9", "surface": "#FFFFFF", "surface_alt": "#F1EEF8",
            "sidebar": "#EDE9F5", "header": "#F5F4F9", "text": "#29243B",
            "muted": "#6A627B", "accent": "#6548B9", "accent_hover": "#51349D",
            "accent_text": "#FFFFFF", "border": "#DDD7EA", "selected": "#E2D9F6",
            "success": "#23734E", "warning": "#946113", "danger": "#B53D57",
            "soft_green_border": "#9BCFBD",
            "log_bg": "#FCFBFE", "log_fg": "#3C354E", "thumb_bg": "#F1EEF8",
        },
    },
    "light": {
        "label": "Sáng", "description": "Trắng và xám xanh, tập trung vào nội dung.",
        "swatch": "#405570",
        "colors": {
            "bg": "#F4F6F8", "surface": "#FFFFFF", "surface_alt": "#EDF1F5",
            "sidebar": "#E9EEF3", "header": "#F4F6F8", "text": "#202E3E",
            "muted": "#596A7D", "accent": "#405570", "accent_hover": "#2D415B",
            "accent_text": "#FFFFFF", "border": "#D7E0E8", "selected": "#DBE5EF",
            "success": "#23734E", "warning": "#946113", "danger": "#B43D4A",
            "soft_green_border": "#9BCFBD",
            "log_bg": "#FCFDFE", "log_fg": "#304254", "thumb_bg": "#EDF1F5",
        },
    },
    "dark": {
        "label": "Tối", "description": "Nền than, chữ sáng và sắc tím dịu cho phòng tối.",
        "swatch": "#292733",
        "colors": {
            "bg": "#1B1923", "surface": "#25222F", "surface_alt": "#302B3E",
            "sidebar": "#211E2A", "header": "#1B1923", "text": "#F3EFFB",
            "muted": "#BDB4D0", "accent": "#BCA4FA", "accent_hover": "#CDBAFC",
            "accent_text": "#241837", "border": "#474054", "selected": "#3E3356",
            "success": "#83D5AD", "warning": "#EBBD77", "danger": "#F197AD",
            "soft_green_border": "#78BDA5",
            "log_bg": "#201D29", "log_fg": "#DAD3E9", "thumb_bg": "#302B3E",
        },
    },
    "brown": {
        "label": "Nâu", "description": "Nền kem ấm, nâu trầm và các bề mặt nhẹ.",
        "swatch": "#775230",
        "colors": {
            "bg": "#F8F5F0", "surface": "#FFFDFA", "surface_alt": "#F2EBE0",
            "sidebar": "#EEE5D7", "header": "#F8F5F0", "text": "#3D3024",
            "muted": "#75624F", "accent": "#775230", "accent_hover": "#654526",
            "accent_text": "#FFFFFF", "border": "#DFD2C0", "selected": "#E8D8C1",
            "success": "#4A7040", "warning": "#966014", "danger": "#AF453D",
            "soft_green_border": "#9BCFBD",
            "log_bg": "#FEFCF8", "log_fg": "#534231", "thumb_bg": "#F2EBE0",
        },
    },
}

PNG_MODERN_THEME = UITheme(
    key=DEFAULT_THEME, label="PNG Modern",
    description="Một không gian gọn, rõ và tập trung cho PNG → PSD → Mockup.",
    **COLOR_PRESETS[DEFAULT_COLOR_PRESET]["colors"],
)
UI_THEMES = {DEFAULT_THEME: PNG_MODERN_THEME}
THEME_LABEL_TO_KEY = {PNG_MODERN_THEME.label: DEFAULT_THEME}
COLOR_PRESET_LABEL_TO_KEY = {item["label"]: key for key, item in COLOR_PRESETS.items()}
THEME_LOAD_ERRORS: list[str] = []

def normalize_theme(value: object) -> str:
    return DEFAULT_THEME

def recommended_color_preset(theme_key: object = DEFAULT_THEME) -> str:
    return DEFAULT_COLOR_PRESET

def normalize_color_preset(value: object, theme_key: object = DEFAULT_THEME) -> str:
    key = str(value or "").strip().casefold().replace(" ", "_")
    aliases = {"purple": "soft_purple", "softpurple": "soft_purple", "tím_nhẹ": "soft_purple",
               "sáng": "light", "tối": "dark", "nâu": "brown", "grey": "light"}
    key = aliases.get(key, key)
    return key if key in COLOR_PRESETS else DEFAULT_COLOR_PRESET

def apply_color_preset(theme: UITheme, preset_key: object) -> UITheme:
    key = normalize_color_preset(preset_key)
    colors = COLOR_PRESETS[key]["colors"]
    return replace(theme, color_preset=key, card_palette=(colors["surface"],), **colors)

def reload_themes(theme_root: Path | None = None) -> tuple[str, ...]:
    """No user-supplied style packages in PNG Mockup 1.0."""
    UI_THEMES.clear()
    UI_THEMES[DEFAULT_THEME] = PNG_MODERN_THEME
    THEME_LABEL_TO_KEY.clear()
    THEME_LABEL_TO_KEY[PNG_MODERN_THEME.label] = DEFAULT_THEME
    THEME_LOAD_ERRORS.clear()
    return ()

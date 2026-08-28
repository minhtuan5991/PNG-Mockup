from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "App Source" / "src"))

import ui_themes


def contrast(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4 for part in channels]
        return sum(part * weight for part, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


class UIThemeTests(unittest.TestCase):
    def test_only_the_approved_style_and_five_palettes_are_exposed(self) -> None:
        self.assertEqual(ui_themes.reload_themes(), ())
        self.assertEqual(tuple(ui_themes.UI_THEMES), ("png_modern",))
        self.assertEqual(tuple(ui_themes.COLOR_PRESETS), ("mint", "soft_purple", "light", "dark", "brown"))
        self.assertEqual(ui_themes.DEFAULT_COLOR_PRESET, "soft_purple")

    def test_palettes_change_semantic_tokens_without_changing_the_style(self) -> None:
        source = ui_themes.UI_THEMES["png_modern"]
        identities = set()
        for preset in ui_themes.COLOR_PRESETS:
            with self.subTest(preset=preset):
                themed = ui_themes.apply_color_preset(source, preset)
                self.assertEqual(themed.key, "png_modern")
                self.assertEqual(themed.visual_style, "png_modern")
                self.assertEqual(themed.color_preset, preset)
                self.assertIsNot(themed, source)
                for token in ui_themes.COLOR_FIELDS:
                    self.assertRegex(getattr(themed, token), r"^#[0-9A-Fa-f]{6}$")
                identities.add((themed.bg, themed.sidebar, themed.accent))
        self.assertEqual(len(identities), 5)
        self.assertEqual(source.color_preset, "soft_purple")

    def test_all_visible_text_pairs_meet_wcag_aa_contrast(self) -> None:
        pairs = (
            ("text", "bg"), ("text", "surface"), ("text", "surface_alt"),
            ("muted", "bg"), ("muted", "surface"), ("muted", "sidebar"),
            ("accent", "selected"), ("accent_text", "accent"),
            ("accent_text", "accent_hover"), ("log_fg", "log_bg"),
            ("success", "surface"), ("warning", "surface"), ("danger", "surface"),
        )
        for preset in ui_themes.COLOR_PRESETS:
            theme = ui_themes.apply_color_preset(ui_themes.PNG_MODERN_THEME, preset)
            for foreground, background in pairs:
                with self.subTest(preset=preset, pair=f"{foreground}/{background}"):
                    self.assertGreaterEqual(contrast(getattr(theme, foreground), getattr(theme, background)), 4.5)

    def test_unsupported_style_or_palette_falls_back_to_png_defaults(self) -> None:
        self.assertEqual(ui_themes.normalize_theme("missing-style"), "png_modern")
        self.assertEqual(ui_themes.normalize_color_preset("missing-palette"), "soft_purple")
        self.assertEqual(ui_themes.normalize_color_preset("Tím nhẹ"), "soft_purple")
        self.assertEqual(ui_themes.normalize_color_preset("Tối"), "dark")


if __name__ == "__main__":
    unittest.main()

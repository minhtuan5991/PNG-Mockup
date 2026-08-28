"""Build the PNG Mockup application icon from simple vector geometry."""
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "App Source" / "assets"


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
  <title>PNG Mockup</title>
  <rect x="8" y="8" width="240" height="240" rx="56" fill="#7045D5"/>
  <rect x="39" y="40" width="152" height="130" rx="19" fill="#D8CCFA"/>
  <rect x="61" y="72" width="157" height="143" rx="19" fill="#FFFFFF"/>
  <circle cx="172" cy="111" r="14" fill="#7045D5"/>
  <path d="M78 187L119 133L146 165L162 148L201 187Z" fill="#7045D5"/>
</svg>
'''
    (ASSETS / "app_icon.svg").write_text(svg, encoding="utf-8")
    scale = 4
    im = Image.new("RGBA", (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(im)

    def rounded(box: tuple[int, int, int, int], radius: int, fill: str) -> None:
        draw.rounded_rectangle(tuple(n * scale for n in box), radius * scale, fill)

    rounded((8, 8, 248, 248), 56, "#7045D5")
    rounded((39, 40, 191, 170), 19, "#D8CCFA")
    rounded((61, 72, 218, 215), 19, "#FFFFFF")
    draw.ellipse(tuple(n * scale for n in (158, 97, 186, 125)), fill="#7045D5")
    draw.polygon(
        [(x * scale, y * scale) for x, y in ((78, 187), (119, 133), (146, 165), (162, 148), (201, 187))],
        fill="#7045D5",
    )
    icon = im.resize((256, 256), Image.Resampling.LANCZOS)
    icon.save(ASSETS / "app_icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    im.resize((512, 512), Image.Resampling.LANCZOS).save(ASSETS / "app_icon.png")
    print("Built independent PNG Mockup SVG, PNG and ICO assets.")


if __name__ == "__main__":
    main()

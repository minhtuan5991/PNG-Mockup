"""Build dedicated Windows icons for PNG Mockup maintenance helpers."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ASSETS = (ROOT / "App Source" / "assets").resolve()
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
MASTER_SIZE = 512


def _owned(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.parent != ASSETS:
        raise ValueError(f"Helper icon destination escaped the asset directory: {resolved}")
    return resolved


def _prepare(source: Path, stem: str) -> tuple[Path, Path]:
    source = source.resolve(strict=True)
    png_path = _owned(ASSETS / f"{stem}.png")
    ico_path = _owned(ASSETS / f"{stem}.ico")
    with Image.open(source) as image:
        image.load()
        if image.width != image.height or image.width < 256:
            raise ValueError(f"{source.name} must be a square image of at least 256 x 256 pixels.")
        rgb = image.convert("RGB").resize((MASTER_SIZE, MASTER_SIZE), Image.Resampling.LANCZOS)
        rgb.save(png_path, format="PNG", optimize=True)
        rgba = image.convert("RGBA").resize((MASTER_SIZE, MASTER_SIZE), Image.Resampling.LANCZOS)
        rgba.save(ico_path, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    return png_path, ico_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--technical", type=Path, required=True)
    parser.add_argument("--uninstall", type=Path, required=True)
    args = parser.parse_args()
    ASSETS.mkdir(parents=True, exist_ok=True)
    outputs = (
        *_prepare(args.technical, "technical_files_icon"),
        *_prepare(args.uninstall, "uninstall_icon"),
    )
    for path in outputs:
        print(f"{path.name}  {path.stat().st_size}  {_digest(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

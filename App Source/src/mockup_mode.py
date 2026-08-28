from __future__ import annotations

from pathlib import Path


PSD_MOCKUP_EXTS = (".psd", ".psb")
RASTER_MOCKUP_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif")
MOCKUP_FILE_EXTS = RASTER_MOCKUP_EXTS + PSD_MOCKUP_EXTS


def image_sides_from_path(path: str | Path) -> int:
    stem = Path(path).stem.strip().casefold()
    parts = [part.strip().casefold() for part in stem.split(".") if part.strip()]
    if parts and parts[-1] == "2s":
        return 2
    if stem.endswith("2s"):
        return 2
    return 1


def mockup_sides_from_path(path: str | Path) -> int:
    return image_sides_from_path(path)


def psd_sides_from_path(path: str | Path) -> int:
    return image_sides_from_path(path)


def explicit_sides_from_path(path: str | Path) -> int | None:
    """Return the declared mockup side count, or None when .1S/.2S is absent."""
    stem = Path(path).stem.strip().casefold()
    parts = [part.strip() for part in stem.split(".") if part.strip()]
    if not parts:
        return None
    if parts[-1] == "1s":
        return 1
    if parts[-1] == "2s":
        return 2
    return None


def is_psd_mockup(path: str | Path) -> bool:
    return Path(path).suffix.casefold() in PSD_MOCKUP_EXTS

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from config import AppConfig, PNG_IMAGE_EXTS, load_config
from mockup_mode import PSD_MOCKUP_EXTS, explicit_sides_from_path
from psd_mockup import find_photoshop_exe, find_photoshop_progids


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    required: bool
    message: str

    @property
    def level(self) -> str:
        return "OK" if self.ok else "FAIL" if self.required else "WARN"


def run_checks(config: AppConfig | None = None) -> list[CheckResult]:
    try:
        cfg = config or load_config()
    except Exception as exc:
        return [CheckResult("config.json", False, True, str(exc))]
    results = [
        CheckResult("config.json", True, True, str(cfg.config_path)),
        CheckResult("Python", sys.version_info >= (3, 12), True,
                    f"{platform.python_version()} at {sys.executable}"),
    ]
    for folder in (cfg.png_dir, cfg.mockup_dir, cfg.output_dir, cfg.logs_dir):
        results.append(CheckResult(f"Folder {folder.name}", folder.is_dir(), True, str(folder)))
    for json_path in (cfg.config_path, cfg.queue_path, cfg.state_path):
        if json_path.exists():
            try:
                json.loads(json_path.read_text(encoding="utf-8-sig"))
                results.append(CheckResult(f"JSON {json_path.name}", True, True, str(json_path)))
            except (OSError, ValueError) as exc:
                results.append(CheckResult(f"JSON {json_path.name}", False, True, str(exc)))

    png_count = len(_image_paths(cfg.png_dir, PNG_IMAGE_EXTS))
    mockups = _image_paths(cfg.mockup_dir, PSD_MOCKUP_EXTS)
    tagged = [path for path in mockups if explicit_sides_from_path(path) is not None]
    untagged = [path for path in mockups if explicit_sides_from_path(path) is None]
    results.append(CheckResult("PNG designs", True, False, f"{png_count} PNG file(s) in {cfg.png_dir}"))
    results.append(CheckResult(
        "Mockup naming .1S/.2S", not untagged, False,
        f"{len(tagged)} tagged; {len(untagged)} skipped without .1S/.2S"
        + (": " + ", ".join(path.name for path in untagged[:8]) if untagged else ""),
    ))
    psd_required = png_count > 0
    results.append(CheckResult(
        "PSD mockup for PNG", bool(tagged) or not psd_required, psd_required,
        f"{len(tagged)} PSD/PSB file(s) tagged .1S/.2S"
        if tagged else "Add a PSD/PSB mockup tagged .1S/.2S to Mockup.",
    ))
    results.append(_photoshop_check(psd_required))
    try:
        import PIL.Image  # noqa: F401
        results.append(CheckResult("Pillow", True, True, "Python package is installed"))
    except Exception as exc:
        results.append(CheckResult("Pillow", False, True, f"{exc}. Reinstall PNG Mockup."))
    return results


def _photoshop_check(required: bool) -> CheckResult:
    """Inspect Photoshop executable and COM registration without launching it."""
    photoshop_exe = find_photoshop_exe()
    photoshop_progids = find_photoshop_progids()
    photoshop_ready = photoshop_exe is not None and bool(photoshop_progids)
    if photoshop_exe is None:
        message = "not found; Adobe Photoshop is required for PNG mockup rendering"
    elif photoshop_progids:
        message = f"{photoshop_exe}; COM: {', '.join(photoshop_progids)}"
    else:
        message = (
            f"{photoshop_exe}; Photoshop COM automation is not registered. "
            "Open Photoshop once with this Windows user and finish first-run/license setup; "
            "if it is still missing, Repair or reinstall Photoshop with Adobe Creative Cloud."
        )
    return CheckResult("Adobe Photoshop", photoshop_ready, required, message)


def has_fatal_failures(results: list[CheckResult]) -> bool:
    return any(not item.ok and item.required for item in results)


def print_results(results: list[CheckResult]) -> None:
    for item in results:
        print(f"[{item.level}] {item.name}: {item.message}")


def _image_paths(folder: Path, extensions: tuple[str, ...]) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(
        (path for path in folder.iterdir() if path.is_file() and path.suffix.casefold() in extensions),
        key=lambda path: path.name.casefold(),
    )


def main() -> int:
    results = run_checks()
    print_results(results)
    return 1 if has_fatal_failures(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

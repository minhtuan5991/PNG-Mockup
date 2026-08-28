from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG: dict[str, Any] = {
    "ui_theme": "png_modern",
    "ui_color_preset": "soft_purple",
    "generation_timeout_sec": 600,
    "output_width_px": 2000,
    "output_height_px": 2000,
    "output_dpi": 72,
    "remove_output_metadata": True,
    "output_watermark_enabled": False,
    "output_watermark_path": "",
    "cleanup_on_exit": False,
    "completion_notification_enabled": True,
    "completion_sound_enabled": False,
    "update_github_repo": "",
    "update_check_on_start": False,
    "update_check_interval_hours": 24,
    "rerun_done": False,
}

SUPPORTED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif")
PNG_IMAGE_EXTS = (".png",)
COLOR_PRESETS = ("mint", "soft_purple", "light", "dark", "brown")


def app_temp_root(project_root: Path) -> Path:
    """Keep each installation's Photoshop work separate from every other app."""
    identity = str(Path(project_root).resolve()).casefold().encode("utf-8")
    install_id = hashlib.sha256(identity).hexdigest()[:20]
    return Path(tempfile.gettempdir()).resolve() / "PNGMockup" / install_id


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    config_path: Path
    ui_theme: str
    ui_color_preset: str
    generation_timeout_sec: int
    output_width_px: int
    output_height_px: int
    output_dpi: int
    remove_output_metadata: bool
    output_watermark_enabled: bool
    output_watermark_path: Path | None
    cleanup_on_exit: bool
    completion_notification_enabled: bool
    completion_sound_enabled: bool
    update_github_repo: str
    update_check_on_start: bool
    update_check_interval_hours: int
    rerun_done: bool

    @property
    def png_dir(self) -> Path:
        return self.project_root / "PNG"

    @property
    def mockup_dir(self) -> Path:
        return self.project_root / "Mockup"

    @property
    def output_dir(self) -> Path:
        return self.project_root / "Output"

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"

    @property
    def queue_path(self) -> Path:
        return self.logs_dir / "queue.json"

    @property
    def state_path(self) -> Path:
        return self.logs_dir / "state.json"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "run.log"

    @property
    def stop_path(self) -> Path:
        return self.logs_dir / "stop.request"

    @property
    def temp_root(self) -> Path:
        return app_temp_root(self.project_root)

    def ensure_folders(self) -> None:
        for folder in (self.png_dir, self.mockup_dir, self.output_dir, self.logs_dir):
            folder.mkdir(parents=True, exist_ok=True)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    path = Path(config_path) if config_path else PROJECT_ROOT / "config.json"
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a JSON object: {path}")
    data = DEFAULT_CONFIG | raw
    root = path.parent
    return AppConfig(
        project_root=root,
        config_path=path,
        ui_theme="png_modern",
        ui_color_preset=_ui_color_preset(data.get("ui_color_preset")),
        generation_timeout_sec=_positive_int(data["generation_timeout_sec"], "generation_timeout_sec"),
        output_width_px=_positive_int(data["output_width_px"], "output_width_px"),
        output_height_px=_positive_int(data["output_height_px"], "output_height_px"),
        output_dpi=_positive_int(data["output_dpi"], "output_dpi"),
        remove_output_metadata=bool(data["remove_output_metadata"]),
        output_watermark_enabled=bool(data["output_watermark_enabled"]),
        output_watermark_path=_optional_path(data["output_watermark_path"], root),
        cleanup_on_exit=bool(data["cleanup_on_exit"]),
        completion_notification_enabled=bool(data["completion_notification_enabled"]),
        completion_sound_enabled=bool(data["completion_sound_enabled"]),
        update_github_repo=str(data["update_github_repo"]).strip(),
        update_check_on_start=bool(data["update_check_on_start"]),
        update_check_interval_hours=_bounded_int(
            data["update_check_interval_hours"], "update_check_interval_hours", 1, 720
        ),
        rerun_done=bool(data["rerun_done"]),
    )


def _resolve_path(value: str, root: Path) -> Path:
    expanded = os.path.expanduser(os.path.expandvars(str(value)))
    path = Path(expanded)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _optional_path(value: Any, root: Path) -> Path | None:
    selected = str(value or "").strip()
    return _resolve_path(selected, root) if selected else None


def _positive_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    number = _positive_int(value, name)
    if number < minimum or number > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _ui_color_preset(value: Any) -> str:
    selected = str(value or "soft_purple").strip().lower().replace(" ", "_")
    if selected == "auto":
        return "soft_purple"
    if selected not in COLOR_PRESETS:
        raise ValueError("ui_color_preset must be mint, soft_purple, light, dark or brown")
    return selected

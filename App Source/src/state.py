from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atomic_json import atomic_write_json as _atomic_write_json
from config import AppConfig
from queue_manager import is_png_job


State = dict[str, Any]
Job = dict[str, Any]


def load_state(config: AppConfig) -> State:
    if not config.state_path.exists():
        return {"version": 1, "jobs": {}, "updated_at": None}
    data = json.loads(config.state_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("jobs", {}), dict):
        raise ValueError(f"State must contain a JSON jobs object: {config.state_path}")
    data.setdefault("version", 1)
    data["jobs"] = {key: job for key, job in data.get("jobs", {}).items() if is_png_job(job)}
    return data


def save_state(config: AppConfig, state: State) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    state["jobs"] = {key: job for key, job in state.get("jobs", {}).items() if is_png_job(job)}
    _atomic_write_json(config.state_path, state)


def apply_state_to_queue(config: AppConfig, jobs: list[Job], state: State) -> list[Job]:
    jobs = [job for job in jobs if is_png_job(job)]
    state_jobs = {key: job for key, job in state.get("jobs", {}).items() if is_png_job(job)}
    by_output = {
        item.get("output_name"): item
        for item in state_jobs.values()
        if item.get("output_name")
    }
    for job in jobs:
        record = state_jobs.get(job["id"])
        if isinstance(record, dict) and not _same_job_record(record, job):
            record = None
        fallback = by_output.get(job["output_name"])
        if isinstance(fallback, dict) and not _same_job_record(fallback, job):
            fallback = None
        record = record or fallback
        if isinstance(record, dict):
            job["status"] = record.get("status", job["status"])
            job["attempts"] = int(record.get("attempts", job.get("attempts", 0)))
            job["last_error"] = record.get("last_error", job.get("last_error"))
            job["idea_sides"] = int(record.get("idea_sides", job.get("idea_sides", 1)))
            job["mockup_sides"] = int(record.get("mockup_sides", job.get("mockup_sides", 1)))
        if job["status"] == "running":
            job["status"] = "pending"
        output_exists = _job_output_exists(config, job)
        if output_exists and not config.rerun_done:
            job["status"] = "done"
            job["last_error"] = None
        elif job["status"] == "done" and not output_exists and not config.rerun_done:
            job["status"] = "pending"
            job["last_error"] = "Output file missing; queued for rerun."
        if config.rerun_done and job["status"] == "done":
            job["status"] = "pending"
            job["last_error"] = None
    return jobs


def record_job(config: AppConfig, state: State, job: Job) -> None:
    if not is_png_job(job):
        raise ValueError("Only PNG/PSD jobs can be recorded by PNG Mockup.")
    state.setdefault("jobs", {})
    state["jobs"][job["id"]] = {
        "id": job["id"],
        "runner": "psd",
        "idea_path": job["idea_path"],
        "artwork_paths": job.get("artwork_paths", [job["idea_path"]]),
        "source_kind": "png",
        "png_front_back_pair": bool(job.get("png_front_back_pair", False)),
        "idea_sides": int(job.get("idea_sides", 1)),
        "mockup_path": job["mockup_path"],
        "mockup_sides": int(job.get("mockup_sides", 1)),
        "psd_output_prefix": job.get("psd_output_prefix"),
        "psd_replacements": job.get("psd_replacements"),
        "output_name": job["output_name"],
        "status": job["status"],
        "attempts": int(job.get("attempts", 0)),
        "last_error": job.get("last_error"),
        "updated_at": _now(),
    }
    save_state(config, state)


def _same_job_record(record: Job, job: Job) -> bool:
    if not is_png_job(record) or not is_png_job(job):
        return False
    if record.get("output_name") != job.get("output_name"):
        return False
    return _norm_artwork_paths(record) == _norm_artwork_paths(job) and _norm_path(
        record.get("mockup_path", "")
    ) == _norm_path(job.get("mockup_path", ""))


def _norm_path(value: Any) -> str:
    try:
        return str(Path(str(value)).resolve()).casefold()
    except Exception:
        return str(value).casefold()


def _norm_artwork_paths(job: Job) -> str:
    paths = job.get("artwork_paths")
    if isinstance(paths, list) and paths:
        return "||".join(_norm_path(path) for path in paths)
    return _norm_path(job.get("idea_path", ""))


def _job_output_exists(config: AppConfig, job: Job) -> bool:
    if not is_png_job(job):
        return False
    marker = _psd_done_marker(config, job)
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return True
    output_paths = data.get("output_paths", [])
    if not isinstance(output_paths, list) or not output_paths:
        return True
    return all(Path(str(path)).exists() for path in output_paths)


def _psd_done_marker(config: AppConfig, job: Job) -> Path:
    stem = Path(str(job.get("output_name", job.get("id", "psd_job")))).stem
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem).strip("._")
    return config.logs_dir / "psd_done" / f"{safe or 'psd_job'}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

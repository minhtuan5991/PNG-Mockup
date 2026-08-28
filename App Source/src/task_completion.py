from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal


RUN_SUMMARY_PREFIX = "PNGMOCKUP_RUN_SUMMARY:"
NoticeLevel = Literal["success", "warning", "error"]


@dataclass(frozen=True)
class RunSummary:
    completed: int
    failed: int
    stopped: bool = False

    @property
    def attempted(self) -> int:
        return self.completed + self.failed


@dataclass(frozen=True)
class CompletionNotice:
    title: str
    message: str
    level: NoticeLevel


def encode_run_summary(summary: RunSummary) -> str:
    payload = {
        "completed": max(0, int(summary.completed)),
        "failed": max(0, int(summary.failed)),
        "stopped": bool(summary.stopped),
    }
    return RUN_SUMMARY_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def parse_run_summary(line: str) -> RunSummary | None:
    marker_index = line.find(RUN_SUMMARY_PREFIX)
    if marker_index < 0:
        return None
    payload = line[marker_index + len(RUN_SUMMARY_PREFIX) :].strip()
    try:
        raw = json.loads(payload)
        completed = int(raw["completed"])
        failed = int(raw["failed"])
        if completed < 0 or failed < 0:
            return None
        return RunSummary(
            completed=completed,
            failed=failed,
            stopped=bool(raw.get("stopped", False)),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def format_elapsed(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    if rounded == 0:
        return "chưa đến 1 giây"
    if rounded < 60:
        return f"{rounded} giây"

    minutes, remaining_seconds = divmod(rounded, 60)
    if minutes < 60:
        if remaining_seconds:
            return f"{minutes} phút {remaining_seconds} giây"
        return f"{minutes} phút"

    hours, remaining_minutes = divmod(minutes, 60)
    if remaining_minutes:
        return f"{hours} giờ {remaining_minutes} phút"
    return f"{hours} giờ"


def build_completion_notice(
    label: str,
    exit_code: int,
    elapsed_seconds: float,
    summary: RunSummary | None,
    *,
    forced: bool = False,
) -> CompletionNotice | None:
    task_label = label.strip() or "Tác vụ"
    elapsed = format_elapsed(elapsed_seconds)
    counts = _format_counts(summary)

    if forced:
        detail = f" sau {elapsed}"
        if counts:
            detail += f" ({counts})"
        return CompletionNotice(
            title="Đã buộc dừng tác vụ",
            message=(
                f"{task_label} đã bị buộc dừng{detail}. "
                "Job đang xử lý có thể cần thực hiện lại."
            ),
            level="warning",
        )

    if exit_code != 0:
        detail = f" sau {elapsed}"
        if counts:
            detail += f" ({counts})"
        return CompletionNotice(
            title="Tác vụ gặp lỗi",
            message=(
                f"{task_label} kết thúc với mã lỗi {exit_code}{detail}. "
                "Mở Nhật ký chạy để xem chi tiết."
            ),
            level="error",
        )

    # A successful process without a runner summary means that no job was
    # attempted (for example, the queue had no pending work). Avoid showing a
    # misleading "completed" notification in that case.
    if summary is None:
        return None

    if summary.stopped:
        if counts:
            message = (
                f"{task_label} đã dừng sau job hiện tại: {counts} trong {elapsed}."
            )
        else:
            message = f"{task_label} đã dừng trước khi bắt đầu job mới sau {elapsed}."
        return CompletionNotice(
            title="Đã dừng theo yêu cầu",
            message=message,
            level="warning" if summary.failed == 0 else "error",
        )

    if summary.failed:
        return CompletionNotice(
            title="Hoàn tất với job lỗi",
            message=(
                f"{task_label}: {counts} trong {elapsed}. "
                "Mở Nhật ký chạy để kiểm tra các job lỗi."
            ),
            level="error",
        )

    if summary.completed:
        return CompletionNotice(
            title="Tác vụ đã hoàn tất",
            message=f"{task_label}: {counts} trong {elapsed}.",
            level="success",
        )

    return None


def play_system_notification(level: NoticeLevel) -> bool:
    if os.name != "nt":
        return False
    try:
        import winsound
    except ImportError:
        return False

    aliases = {
        "success": "SystemAsterisk",
        "warning": "SystemExclamation",
        "error": "SystemHand",
    }
    beep_types = {
        "success": winsound.MB_ICONASTERISK,
        "warning": winsound.MB_ICONEXCLAMATION,
        "error": winsound.MB_ICONHAND,
    }
    flags = (
        winsound.SND_ALIAS
        | winsound.SND_ASYNC
        | getattr(winsound, "SND_NODEFAULT", 0)
    )
    try:
        winsound.PlaySound(aliases[level], flags)
        return True
    except (OSError, RuntimeError):
        try:
            winsound.MessageBeep(beep_types[level])
            return True
        except (OSError, RuntimeError):
            return False


def _format_counts(summary: RunSummary | None) -> str:
    if summary is None:
        return ""
    parts: list[str] = []
    if summary.completed:
        parts.append(f"{summary.completed} job thành công")
    if summary.failed:
        parts.append(f"{summary.failed} job lỗi")
    return ", ".join(parts)

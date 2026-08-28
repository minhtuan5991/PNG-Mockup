from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


REPLACE_RETRY_TIMEOUT_SEC = 5.0
_INITIAL_RETRY_DELAY_SEC = 0.025
_MAX_RETRY_DELAY_SEC = 0.25
_WINDOWS_LOCK_ERRORS = {5, 32, 33}


def _preserve_windows_visibility(destination: Path, staging: Path) -> None:
    """Keep a hidden technical JSON file hidden after atomic replacement.

    Read-only permissions are deliberately not cleared. A write to a read-only
    destination must still fail, rather than silently bypassing user intent.
    """
    if os.name != "nt":
        return
    try:
        visibility = destination.stat().st_file_attributes & 0x6  # Hidden | System
    except FileNotFoundError:
        return
    if not visibility:
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    setter = kernel.SetFileAttributesW
    setter.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    setter.restype = wintypes.BOOL
    attributes = (staging.stat().st_file_attributes & ~0x80) | visibility
    if not setter(str(staging), attributes):
        raise ctypes.WinError(ctypes.get_last_error())


def atomic_write_json(path: Path, data: Any) -> None:
    """Publish complete JSON, tolerating short Windows reader/scanner locks.

    Each writer owns its staging file. Never delete/truncate the destination
    as a fallback: if publication fails, its last valid contents must survive.
    Persistent permission errors still propagate after a bounded retry window.
    """
    staging_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as file:
            staging_path = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        _preserve_windows_visibility(path, staging_path)
        deadline = time.monotonic() + REPLACE_RETRY_TIMEOUT_SEC
        delay = _INITIAL_RETRY_DELAY_SEC
        while True:
            try:
                staging_path.replace(path)
                return
            except OSError as exc:
                remaining = deadline - time.monotonic()
                retryable = isinstance(exc, PermissionError) or (
                    getattr(exc, "winerror", None) in _WINDOWS_LOCK_ERRORS
                )
                if not retryable or remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, _MAX_RETRY_DELAY_SEC)
    finally:
        if staging_path is not None:
            try:
                staging_path.unlink(missing_ok=True)
            except OSError:
                # A scanner may also hold the staging file. Do not mask the
                # original publication error; normal app cleanup can remove it.
                pass

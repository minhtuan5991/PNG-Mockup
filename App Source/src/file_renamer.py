from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from config import SUPPORTED_IMAGE_EXTS
from mockup_mode import PSD_MOCKUP_EXTS


COLOR_MARKERS = {"bl", "wh"}
SIDE_MARKERS = {"f", "b"}
MOCKUP_SIDE_MARKERS = {"1s", "2s"}
NAME_MARKERS = COLOR_MARKERS | SIDE_MARKERS | MOCKUP_SIDE_MARKERS
RENAMABLE_FILE_EXTS = SUPPORTED_IMAGE_EXTS + PSD_MOCKUP_EXTS
COPY_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")


class RenameConflictError(ValueError):
    pass


@dataclass(frozen=True)
class RenameOperation:
    source: Path
    target: Path


def renameable_files(
    folder: Path, extensions: tuple[str, ...] = SUPPORTED_IMAGE_EXTS
) -> list[Path]:
    if not folder.exists():
        return []
    allowed = {extension.casefold() for extension in extensions}
    return sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.casefold() in allowed
        ),
        key=lambda path: path.name.casefold(),
    )


def renamed_path(
    source: Path,
    color: str | None,
    side: str | None,
    mockup_sides: str | None = None,
) -> Path:
    color = color.casefold() if color else None
    side = side.casefold() if side else None
    mockup_sides = mockup_sides.casefold() if mockup_sides else None
    if color not in COLOR_MARKERS | {None}:
        raise ValueError(f"Nhãn màu không hợp lệ: {color}")
    if side not in SIDE_MARKERS | {None}:
        raise ValueError(f"Nhãn mặt áo không hợp lệ: {side}")
    if mockup_sides not in MOCKUP_SIDE_MARKERS | {None}:
        raise ValueError(f"Nhãn số mặt mockup không hợp lệ: {mockup_sides}")

    file_extension = (
        source.suffix if source.suffix.casefold() in RENAMABLE_FILE_EXTS else ""
    )
    raw_stem = source.name[: -len(file_extension)] if file_extension else source.name
    base_name = _base_name(raw_stem)
    is_psd = source.suffix.casefold() in PSD_MOCKUP_EXTS
    if is_psd and mockup_sides is None:
        raise ValueError(f"File PSD cần chọn Mockup 1 Mặt hoặc Mockup 2 Mặt: {source.name}")
    markers = [marker for marker in (() if is_psd else (color, side)) if marker]
    if mockup_sides:
        markers.append("1S" if mockup_sides == "1s" else "2S")
    marker_text = "".join(f".{marker}" for marker in markers)
    return source.with_name(f"{base_name}{marker_text}{file_extension}")


def build_rename_plan(
    sources: Iterable[Path],
    color: str | None,
    side: str | None,
    mockup_sides: str | None = None,
) -> list[RenameOperation]:
    operations = [
        RenameOperation(
            source=source,
            target=renamed_path(source, color, side, mockup_sides),
        )
        for source in sources
    ]
    if not operations:
        raise ValueError("Chưa chọn file cần đổi tên.")

    source_keys = {_path_key(operation.source) for operation in operations}
    target_keys: dict[str, Path] = {}
    for operation in operations:
        key = _path_key(operation.target)
        previous = target_keys.get(key)
        if previous is not None:
            raise RenameConflictError(
                "Nhiều file sẽ có cùng tên sau khi đổi:\n"
                f"- {previous.name}\n"
                f"- {operation.source.name}\n"
                f"Tên đích: {operation.target.name}"
            )
        target_keys[key] = operation.source

        if operation.target.exists() and key not in source_keys:
            raise RenameConflictError(
                f"File đích đã tồn tại: {operation.target.name}"
            )
    return operations


def execute_rename_plan(operations: Iterable[RenameOperation]) -> int:
    changes = [
        operation
        for operation in operations
        if _path_key(operation.source) != _path_key(operation.target)
    ]
    if not changes:
        return 0

    staged: list[tuple[RenameOperation, Path]] = []
    completed: set[RenameOperation] = set()
    try:
        for operation in changes:
            temporary = operation.source.with_name(
                f".__png_mockup_rename_{uuid.uuid4().hex}{operation.source.suffix}"
            )
            operation.source.rename(temporary)
            staged.append((operation, temporary))

        for operation, temporary in staged:
            temporary.rename(operation.target)
            completed.add(operation)
    except OSError:
        for operation, temporary in reversed(staged):
            current = operation.target if operation in completed else temporary
            if current.exists() and not operation.source.exists():
                try:
                    current.rename(operation.source)
                except OSError:
                    pass
        raise
    return len(changes)


def _base_name(raw_stem: str) -> str:
    base_name = raw_stem.strip()
    previous = None
    while base_name and base_name != previous:
        previous = base_name
        base_name = COPY_SUFFIX_RE.sub("", base_name).strip()
        parts = base_name.split(".")
        while len(parts) > 1 and parts[-1].casefold() in NAME_MARKERS:
            parts.pop()
        base_name = ".".join(parts).strip()
    if not base_name:
        raise ValueError(f"Không xác định được tên gốc từ: {raw_stem}")
    return base_name


def _path_key(path: Path) -> str:
    return str(path.absolute()).casefold()

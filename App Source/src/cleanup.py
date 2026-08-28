from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig, app_temp_root, load_config


@dataclass
class CleanupReport:
    removed_files: int = 0
    freed_bytes: int = 0
    warnings: list[str] = field(default_factory=list)


def cleanup_app_files(config: AppConfig, include_pip_cache: bool = True) -> CleanupReport:
    """Remove only this installation's scratch files; never user images or other apps."""
    report = CleanupReport()
    root = config.project_root.resolve()
    for pattern in ("error_*.png", "*.tmp", "*.part", "*.download"):
        for path in config.logs_dir.glob(pattern):
            _remove_file(path, root, report)
    _trim_log(config.log_path, root, report)
    _clear_temp_root(config.temp_root, root, report)
    source_root = root / "src"
    if _contained(source_root, root) and source_root.is_dir():
        for cache_dir in source_root.rglob("__pycache__"):
            _remove_tree(cache_dir, root, report)
    if include_pip_cache:
        _clear_directory(root / "runtime" / "pip-cache", root, report)
    return report


def format_cleanup_report(report: CleanupReport) -> str:
    message = f"Đã xóa {report.removed_files} file rác, giải phóng khoảng {_format_size(report.freed_bytes)}."
    if report.warnings:
        message += "\n\nMột số mục chưa thể dọn:\n- " + "\n- ".join(report.warnings)
    return message


def _contained(path: Path, allowed_root: Path) -> bool:
    try:
        resolved = path.resolve()
        root = allowed_root.resolve()
        return resolved != root and root in resolved.parents
    except (OSError, RuntimeError):
        return False


def _clear_directory(folder: Path, allowed_root: Path, report: CleanupReport) -> None:
    if not folder.exists():
        return
    if not _contained(folder, allowed_root):
        report.warnings.append(f"Bỏ qua đường dẫn ngoài app: {folder}")
        return
    for child in list(folder.iterdir()):
        if child.is_dir():
            _remove_tree(child, allowed_root, report)
        else:
            _remove_file(child, allowed_root, report)


def _clear_temp_root(temp_root: Path, project_root: Path, report: CleanupReport) -> None:
    expected = app_temp_root(project_root)
    # Resolve before deleting so junctions cannot redirect cleanup to another app.
    if temp_root != expected or temp_root.resolve() != expected.absolute():
        report.warnings.append(f"Bỏ qua thư mục tạm không an toàn: {temp_root}")
        return
    if temp_root.exists():
        _remove_tree(temp_root, expected.parent, report)


def _remove_tree(path: Path, allowed_root: Path, report: CleanupReport) -> None:
    if not _contained(path, allowed_root):
        report.warnings.append(f"Bỏ qua thư mục ngoài vùng dọn: {path}")
        return
    file_count, size = _tree_stats(path)
    try:
        shutil.rmtree(path)
        report.removed_files += file_count
        report.freed_bytes += size
    except OSError as exc:
        report.warnings.append(f"{path}: {exc}")


def _remove_file(path: Path, allowed_root: Path, report: CleanupReport) -> None:
    if not _contained(path, allowed_root):
        report.warnings.append(f"Bỏ qua file ngoài vùng dọn: {path}")
        return
    try:
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        report.removed_files += 1
        report.freed_bytes += size
    except OSError as exc:
        report.warnings.append(f"{path}: {exc}")


def _trim_log(path: Path, allowed_root: Path, report: CleanupReport, keep_lines: int = 300) -> None:
    if not path.exists():
        return
    if not _contained(path, allowed_root):
        report.warnings.append(f"Bỏ qua log ngoài app: {path}")
        return
    try:
        old_size = path.stat().st_size
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        text = "\n".join(lines[-keep_lines:])
        if text:
            text += "\n"
        path.write_text(text, encoding="utf-8")
        report.freed_bytes += max(0, old_size - path.stat().st_size)
    except OSError as exc:
        report.warnings.append(f"{path}: {exc}")


def _tree_stats(path: Path) -> tuple[int, int]:
    count = 0
    size = 0
    if not path.exists():
        return count, size
    for directory, _dirs, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            child = Path(directory) / name
            if not _contained(child, path):
                continue
            try:
                size += child.stat().st_size
                count += 1
            except OSError:
                pass
    return count, size


def _format_size(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MB"
    return f"{value / 1024:.1f} KB"


def main() -> int:
    report = cleanup_app_files(load_config())
    print(format_cleanup_report(report))
    return 0 if not report.warnings else 1


if __name__ == "__main__":
    raise SystemExit(main())

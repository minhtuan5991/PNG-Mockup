from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import AppConfig, app_temp_root
from output_metadata import strip_png_metadata_atomic
from output_watermark import OutputWatermarkError, apply_watermark_atomic


ROOT = Path(__file__).resolve().parents[1]
PHOTOSHOP_JSX = ROOT / "tools" / "photoshop_psd_mockup.jsx"
PHOTOSHOP_GENERIC_PROGID = "Photoshop.Application"
_PHOTOSHOP_VERSIONED_PROGID_RE = re.compile(
    r"^Photoshop\.Application\.(\d+(?:\.\d+)*)$",
    re.IGNORECASE,
)


def is_psd_job(job: dict[str, Any]) -> bool:
    return job.get("runner") == "psd"


def run_psd_job(config: AppConfig, job: dict[str, Any], log: Any) -> None:
    psd_path = Path(str(job["mockup_path"])).resolve()
    if not psd_path.exists():
        raise FileNotFoundError(f"PSD mockup not found: {psd_path}")
    if not PHOTOSHOP_JSX.exists():
        raise FileNotFoundError(f"Missing Photoshop JSX runner: {PHOTOSHOP_JSX}")
    photoshop_exe = find_photoshop_exe()
    if photoshop_exe is None:
        raise RuntimeError(
            "Adobe Photoshop is required to render PNG mockups. Install Photoshop "
            "and open it once to finish license and first-run setup."
        )

    replacements = job.get("psd_replacements")
    if not isinstance(replacements, list) or not replacements:
        raise RuntimeError(f"PSD job has no smart object replacements: {job['output_name']}")

    for replacement in replacements:
        design_path = Path(str(replacement.get("path", ""))).resolve()
        if not design_path.exists():
            raise FileNotFoundError(f"Design file not found for PSD smart layer: {design_path}")
        replacement["path"] = str(design_path)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    temp_root = app_temp_root(config.project_root)
    work_dir = temp_root / "psd_jobs" / _safe_name(
        Path(job["output_name"]).stem
    )
    export_dir = work_dir / "exports"
    if work_dir.resolve() != work_dir.absolute() or export_dir.resolve() != export_dir.absolute():
        raise RuntimeError("PNG Mockup temporary folder must not redirect outside this installation.")
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    result_path = work_dir / "result.json"
    spec_path = work_dir / "job.json"
    temp_jsx_path = work_dir / "run_photoshop.jsx"
    vbs_path = work_dir / "run_photoshop.vbs"
    # A work directory is intentionally reused for the same output name. Never
    # let a result from an earlier attempt be mistaken for the current run.
    result_path.unlink(missing_ok=True)

    output_prefix = _safe_name(str(job.get("psd_output_prefix") or Path(job["output_name"]).stem))
    spec = {
        "psd_path": str(psd_path),
        "export_dir": str(export_dir),
        "output_prefix": output_prefix,
        "replacements": replacements,
        "result_path": str(result_path),
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=True, indent=2), encoding="utf-8")

    template = PHOTOSHOP_JSX.read_text(encoding="utf-8")
    spec_literal = json.dumps(spec, ensure_ascii=True, indent=2)
    temp_jsx_path.write_text(
        "var PSD_MOCKUP_JOB_PATH = " + json.dumps(str(spec_path), ensure_ascii=True) + ";\n"
        + "var PSD_MOCKUP_SPEC = " + spec_literal + ";\n"
        + template,
        encoding="utf-8-sig",
    )

    progids = _photoshop_progid_candidates()
    vbs_path.write_text(_photoshop_vbs(temp_jsx_path, progids), encoding="utf-8")

    log.info("Running PSD mockup via Photoshop: %s", psd_path)
    log.info("Photoshop COM candidates: %s", ", ".join(progids))
    completed = subprocess.run(
        ["cscript.exe", "//nologo", str(vbs_path)],
        cwd=config.project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(int(config.generation_timeout_sec), 900),
    )
    photoshop_output = completed.stdout.strip()
    if photoshop_output:
        log.info("Photoshop output: %s", photoshop_output)
    if completed.returncode == 2:
        # A first launch or Creative Cloud repair can register a versioned
        # ProgID while PNG Mockup is still open. Refresh discovery on the
        # user's next retry instead of retaining the failed registry snapshot.
        find_photoshop_progids.cache_clear()
        detail = f" Chi tiết: {photoshop_output}" if photoshop_output else ""
        raise RuntimeError(
            "Không thể khởi động Photoshop Automation (COM). Hãy mở Photoshop "
            "một lần bằng đúng tài khoản Windows đang chạy PNG Mockup, "
            "hoàn tất màn hình khởi động và giấy phép, đóng Photoshop rồi "
            "thử lại. Nếu vẫn lỗi, hãy Repair hoặc cài lại Photoshop bằng "
            "Adobe Creative Cloud. Không cần tài khoản GitHub cho thao tác này. "
            f"ProgID đã thử: {', '.join(progids)}.{detail}"
        )
    if completed.returncode == 3:
        raise RuntimeError(
            "Photoshop JSX script failed"
            + (f": {photoshop_output}" if photoshop_output else ".")
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Photoshop PSD export failed with exit code {completed.returncode}: "
            f"{photoshop_output}"
        )

    if not result_path.exists():
        raise RuntimeError("Photoshop finished but did not write PSD export result.")

    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "Photoshop PSD export failed."))

    raw_outputs = [Path(str(path)).resolve() for path in result.get("outputs", [])]
    if not raw_outputs:
        raise RuntimeError("Photoshop did not export any PSD mockup image.")

    staged_outputs: list[tuple[Path, Path, Path]] = []
    try:
        for raw_output in raw_outputs:
            if not raw_output.exists():
                raise RuntimeError(f"Photoshop output missing: {raw_output}")
            final_path = config.output_dir / raw_output.name
            file_descriptor, staged_name = tempfile.mkstemp(
                prefix=f".{final_path.name}.postprocess-",
                suffix=final_path.suffix,
                dir=config.output_dir,
            )
            os.close(file_descriptor)
            staged_path = Path(staged_name)
            shutil.copyfile(raw_output, staged_path)
            if bool(getattr(config, "output_watermark_enabled", False)):
                watermark_path = getattr(config, "output_watermark_path", None)
                if watermark_path is None:
                    raise OutputWatermarkError(
                        "Đã bật Gắn WM cho Mockup nhưng chưa chọn ảnh Watermark."
                    )
                apply_watermark_atomic(
                    staged_path,
                    watermark_path,
                    log=log,
                    expected_size=(
                        int(getattr(config, "output_width_px", 2000)),
                        int(getattr(config, "output_height_px", 2000)),
                    ),
                    expected_dpi=int(getattr(config, "output_dpi", 72)),
                )
            if config.remove_output_metadata:
                # This final pass removes Comment, EXIF, XMP, EXIF thumbnail,
                # IPTC and ICC profile, then verifies they are absent.
                strip_png_metadata_atomic(staged_path, log=log)
            staged_outputs.append((raw_output, staged_path, final_path))
    except Exception:
        for _raw_output, staged_path, _final_path in staged_outputs:
            staged_path.unlink(missing_ok=True)
        # Also remove the stage created by the current iteration before it was
        # appended to staged_outputs.
        if "staged_path" in locals():
            staged_path.unlink(missing_ok=True)
        raise

    final_outputs: list[Path] = []
    try:
        for raw_output, staged_path, final_path in staged_outputs:
            os.replace(staged_path, final_path)
            raw_output.unlink(missing_ok=True)
            final_outputs.append(final_path)
    finally:
        for _raw_output, staged_path, _final_path in staged_outputs:
            staged_path.unlink(missing_ok=True)

    marker = psd_done_marker(config, job)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "job_id": job.get("id"),
                "output_name": job.get("output_name"),
                "output_paths": [str(path) for path in final_outputs],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    job["psd_exported_paths"] = [str(path) for path in final_outputs]
    log.info("PSD job exported %s image(s): %s", len(final_outputs), " | ".join(map(str, final_outputs)))


def psd_done_marker(config: AppConfig, job: dict[str, Any]) -> Path:
    stem = Path(str(job.get("output_name", job.get("id", "psd_job")))).stem
    return config.logs_dir / "psd_done" / f"{_safe_name(stem) or 'psd_job'}.json"


def find_photoshop_exe() -> Path | None:
    registry_path = _photoshop_from_registry()
    if registry_path is not None:
        return registry_path

    for base in (Path(r"C:\Program Files\Adobe"), Path(r"C:\Program Files (x86)\Adobe")):
        if not base.exists():
            continue
        try:
            matches = sorted(base.rglob("Photoshop.exe"), key=lambda path: str(path).casefold())
        except OSError:
            matches = []
        if matches:
            return matches[-1]
    return None


def _photoshop_from_registry() -> Path | None:
    try:
        import winreg
    except Exception:
        return None

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe",
        ),
    ]
    for root, subkey in keys:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = Path(str(value))
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1)
def find_photoshop_progids() -> tuple[str, ...]:
    """Return registered Photoshop automation ProgIDs without starting Photoshop."""

    try:
        import winreg
    except Exception:
        return ()
    return _discover_photoshop_progids(winreg)


def _discover_photoshop_progids(winreg: Any) -> tuple[str, ...]:
    registered: dict[str, str] = {}
    current_versions: list[str] = []
    base_access = int(getattr(winreg, "KEY_READ", 0))
    view_flags: list[int] = []
    for flag in (
        int(getattr(winreg, "KEY_WOW64_64KEY", 0)),
        int(getattr(winreg, "KEY_WOW64_32KEY", 0)),
        0,
    ):
        if flag not in view_flags:
            view_flags.append(flag)

    for view_flag in view_flags:
        access = base_access | view_flag
        try:
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT,
                rf"{PHOTOSHOP_GENERIC_PROGID}\CurVer",
                0,
                access,
            ) as key:
                value, _value_type = winreg.QueryValueEx(key, "")
            current = str(value).strip()
            if _is_photoshop_progid(current):
                current_versions.append(current)
            registered.setdefault(PHOTOSHOP_GENERIC_PROGID.casefold(), PHOTOSHOP_GENERIC_PROGID)
        except OSError:
            pass

        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "", 0, access) as root:
                index = 0
                while True:
                    try:
                        name = str(winreg.EnumKey(root, index))
                    except OSError:
                        break
                    index += 1
                    if _is_photoshop_progid(name):
                        registered.setdefault(name.casefold(), name)
        except OSError:
            continue

    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(registered.get(key, value))

    # CurVer identifies the version selected by the Photoshop installer. It is
    # the most reliable first choice when multiple Photoshop releases coexist.
    for current in current_versions:
        add(current)

    versioned = [
        value
        for key, value in registered.items()
        if key != PHOTOSHOP_GENERIC_PROGID.casefold()
        and _PHOTOSHOP_VERSIONED_PROGID_RE.fullmatch(value)
    ]
    for value in sorted(versioned, key=_photoshop_progid_sort_key):
        add(value)

    if PHOTOSHOP_GENERIC_PROGID.casefold() in registered:
        add(PHOTOSHOP_GENERIC_PROGID)
    return tuple(ordered)


def _is_photoshop_progid(value: str) -> bool:
    return value.casefold() == PHOTOSHOP_GENERIC_PROGID.casefold() or bool(
        _PHOTOSHOP_VERSIONED_PROGID_RE.fullmatch(value)
    )


def _photoshop_progid_sort_key(value: str) -> tuple[int, int, tuple[int, ...]]:
    match = _PHOTOSHOP_VERSIONED_PROGID_RE.fullmatch(value)
    if match is None:
        return (0, 0, ())
    parts = tuple(int(part) for part in match.group(1).split("."))
    # Newer major versions first, but prefer the main ProgID (for example .170)
    # over secondary aliases such as .170.1 for the same Photoshop release.
    return (-parts[0], len(parts), tuple(-part for part in parts[1:]))


def _photoshop_progid_candidates() -> tuple[str, ...]:
    candidates = list(find_photoshop_progids())
    if not any(
        value.casefold() == PHOTOSHOP_GENERIC_PROGID.casefold() for value in candidates
    ):
        candidates.append(PHOTOSHOP_GENERIC_PROGID)
    return tuple(candidates)


def _photoshop_vbs(jsx_path: Path, progids: tuple[str, ...] | None = None) -> str:
    quoted_jsx = str(jsx_path).replace('"', '""')
    safe_progids = tuple(
        value for value in (progids or _photoshop_progid_candidates()) if _is_photoshop_progid(value)
    )
    if not safe_progids:
        safe_progids = (PHOTOSHOP_GENERIC_PROGID,)
    progid_array = ", ".join(f'"{value}"' for value in safe_progids)
    return (
        "On Error Resume Next\r\n"
        "Dim app, progIds, progId, connected, tried, lastError\r\n"
        f"progIds = Array({progid_array})\r\n"
        "connected = False\r\n"
        "tried = \"\"\r\n"
        "lastError = \"\"\r\n"
        "For Each progId In progIds\r\n"
        "  If Len(tried) > 0 Then tried = tried & \", \"\r\n"
        "  tried = tried & progId\r\n"
        "  Set app = Nothing\r\n"
        "  Err.Clear\r\n"
        "  Set app = GetObject(, progId)\r\n"
        "  If Err.Number <> 0 Then\r\n"
        "    lastError = Err.Description\r\n"
        "    Err.Clear\r\n"
        "    Set app = CreateObject(progId)\r\n"
        "  End If\r\n"
        "  If Err.Number = 0 Then\r\n"
        "    connected = True\r\n"
        "    Exit For\r\n"
        "  End If\r\n"
        "  lastError = Err.Description\r\n"
        "Next\r\n"
        "If Not connected Then\r\n"
        "  WScript.Echo \"Cannot start Adobe Photoshop COM automation. Tried: \" & tried & \". \" & lastError\r\n"
        "  WScript.Quit 2\r\n"
        "End If\r\n"
        "Err.Clear\r\n"
        "app.Visible = True\r\n"
        f"app.DoJavaScriptFile \"{quoted_jsx}\"\r\n"
        "If Err.Number <> 0 Then\r\n"
        "  WScript.Echo \"Photoshop script failed: \" & Err.Description\r\n"
        "  WScript.Quit 3\r\n"
        "End If\r\n"
        "WScript.Quit 0\r\n"
    )


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value))
    safe = safe.strip("._")
    return safe or "psd_mockup"

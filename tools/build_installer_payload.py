"""Build the offline PNG Mockup payload from App Source, never user data.

The EXE is an unsigned .NET installer stub followed by a ZIP and a 48-byte
footer: little-endian ZIP length, raw SHA-256 digest, and PNGMSP01 marker.
Every ZIP file is listed by hash/length in setup_manifest.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "App Source"
ROOT_FILES = ("PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "config.json", "version.json", "update-channel.json", "README.md", "THIRD_PARTY_NOTICES.md")
TOOL_FILES = ("python.bat", "pythonw.bat", "runtime_check.py", "setup_shortcuts.ps1", "photoshop_psd_mockup.jsx")
PRESETS = {"mint", "soft_purple", "light", "dark", "brown"}
FOOTER = struct.Struct("<q32s8s")


def assert_owned(path: Path, base: Path = ROOT) -> Path:
    absolute = path.absolute()
    resolved = path.resolve()
    if base.resolve() not in resolved.parents:
        raise ValueError(f"Build path is outside the project: {path}")
    for component in (absolute, *absolute.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError(f"Build path passes through a reparse point: {component}")
    return resolved


def walk_files(directory: Path):
    assert_owned(directory)
    for current, children, files in os.walk(directory, followlinks=False):
        current_path = Path(current)
        for name in children:
            child = current_path / name
            if child.is_symlink() or child.is_junction():
                raise ValueError(f"Cannot ship a symbolic link or junction: {child}")
        children[:] = sorted(name for name in children if name not in {"__pycache__", ".pytest_cache", ".git"})
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            yield assert_owned(current_path / name)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def selected_files() -> list[Path]:
    paths = [APP / name for name in ROOT_FILES]
    paths.extend(APP / "tools" / name for name in TOOL_FILES)
    for directory in (
        APP / "src",
        APP / "assets",
        APP / "tools" / "oxipng",
        APP / "tools" / "libwebp",
        APP / "runtime" / "python",
    ):
        paths.extend(walk_files(directory))
    unique: dict[str, Path] = {}
    for path in paths:
        path = assert_owned(path)
        if not path.is_file():
            raise ValueError(f"Required application file is missing: {path}")
        relative = path.relative_to(APP).as_posix()
        if relative.casefold() in unique:
            raise ValueError(f"Duplicate payload path: {relative}")
        if any(part in {"", ".", ".."} or part.endswith((".", " ")) or ":" in part for part in relative.split("/")):
            raise ValueError(f"Invalid archive filename: {relative}")
        unique[relative.casefold()] = path
    return sorted(unique.values(), key=lambda item: item.relative_to(APP).as_posix())


def validate_product() -> dict:
    identity = json.loads((APP / "version.json").read_text(encoding="utf-8-sig"))
    if identity.get("product_id") != "png-mockup" or identity.get("name") != "PNG Mockup":
        raise ValueError("Refusing to build a different product.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", identity.get("version", "")):
        raise ValueError("Invalid application version.")
    config = json.loads(assert_owned(ROOT / "payload-template" / "config.json").read_text(encoding="utf-8-sig"))
    channel = json.loads((APP / "update-channel.json").read_text(encoding="utf-8-sig"))
    if channel.get("product_id") != "png-mockup" or channel.get("schema_version") != 1 or not isinstance(channel.get("enabled"), bool):
        raise ValueError("The update channel must explicitly identify PNG Mockup.")
    repository = channel.get("repository", "")
    if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("The dedicated update repository must use owner/name syntax.")
    if any(legacy in repository.casefold() for legacy in ("automockup", "autodesignpod", "geminimockup")):
        raise ValueError("The original product's repository must not be used.")
    if channel["enabled"] and not repository:
        raise ValueError("Online updates require a dedicated product repository.")
    if config.get("update_github_repo") and config["update_github_repo"] != repository:
        raise ValueError("Default settings refer to a different update repository.")
    if config.get("update_check_on_start") and not channel["enabled"]:
        raise ValueError("Default automatic checks require an enabled product-validated channel.")
    for value in config.values():
        if isinstance(value, str) and (re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\")):
            raise ValueError("Do not distribute machine-specific absolute paths in the default configuration.")
    runtime = APP / "runtime" / "python"
    expected_pth = [".", "DLLs", "Lib", "Lib\\site-packages", "..\\..\\src", "import site"]
    if (runtime / "python312._pth").read_text(encoding="utf-8-sig").splitlines() != expected_pth:
        raise ValueError("The private interpreter search path has changed; review isolation before shipping.")
    packages = [path.name for path in (runtime / "Lib" / "site-packages").iterdir() if path.name != "__pycache__"]
    if len(packages) != 2 or "PIL" not in packages or not any(re.fullmatch(r"pillow-[0-9.]+\.dist-info", name) for name in packages):
        raise ValueError("The runtime contains unexpected third-party packages.")
    import ui_themes
    if set(ui_themes.COLOR_PRESETS) != PRESETS or set(ui_themes.UI_THEMES) != {"png_modern"}:
        raise ValueError("Expected one visual style with exactly five approved color presets.")
    for preset in PRESETS:
        if ui_themes.apply_color_preset(ui_themes.PNG_MODERN_THEME, preset).color_preset != preset:
            raise ValueError(f"Color preset does not load: {preset}")
    return identity


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--stage-only", action="store_true", help="Keep this test build in its unique staging directory.")
    args = parser.parse_args()
    stage = assert_owned(args.stage, ROOT / "build")
    identity = validate_product()
    stub = stage / "installer-stub.exe"
    if not stub.is_file():
        raise ValueError("Compile the installer stub before building its payload.")
    payload = stage / "payload.zip"
    manifest = {"format": 1, "product_id": identity["product_id"], "version": identity["version"], "files": []}
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=False) as archive:
        for path in selected_files():
            relative = path.relative_to(APP).as_posix()
            # App Source/config.json is the developer/user's live preferences.
            # New installs always get the audited distributable defaults.
            source = ROOT / "payload-template" / "config.json" if relative == "config.json" else path
            content = assert_owned(source).read_bytes()
            manifest["files"].append({"path": relative, "length": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            info = zipfile.ZipInfo(relative, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compresslevel=9)
        archive.writestr("setup_manifest.json", json_bytes(manifest), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    (stage / "setup_manifest.json").write_bytes(json_bytes(manifest))
    payload_bytes = payload.read_bytes()
    digest = hashlib.sha256(payload_bytes).digest()
    installer = stage / "PNG Mockup Setup.exe"
    with installer.open("wb") as output:
        with stub.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        output.write(payload_bytes)
        output.write(FOOTER.pack(len(payload_bytes), digest, b"PNGMSP01"))
    report = stage / "installer-verification.json"
    verified = subprocess.run([str(installer), "/verify", "/report", str(report)], cwd=stage, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    result = json.loads(report.read_text(encoding="utf-8-sig"))
    if verified.returncode != 0 or not result.get("ok") or result.get("product_id") != "png-mockup":
        raise RuntimeError(f"Actual installer verification failed: {result}")
    if result.get("payload_sha256") != digest.hex() or result.get("files") != len(manifest["files"]) + 1:
        raise RuntimeError("Installer verification does not match the generated manifest.")
    final = installer if args.stage_only else assert_owned(ROOT / "PNG Mockup Setup.exe")
    if not args.stage_only:
        shutil.copy2(installer, final)
    setup_hash = hashlib.sha256(final.read_bytes()).hexdigest()
    assert_owned(final.with_name(final.name + ".sha256")).write_text(f"{setup_hash}  PNG Mockup Setup.exe\n", encoding="ascii")
    audit = {**result, "installer": str(final), "installer_bytes": final.stat().st_size, "installer_sha256": setup_hash,
             "app_source": str(APP), "color_presets": sorted(PRESETS), "signed": False, "network_used": False,
             "stage_only": args.stage_only}
    (stage / "build-result.json").write_bytes(json_bytes(audit))
    if not args.stage_only:
        assert_owned(ROOT / "verification").mkdir(exist_ok=True)
        assert_owned(ROOT / "verification" / "installer-build.json").write_bytes(json_bytes(audit))
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

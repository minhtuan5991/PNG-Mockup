"""Prepare verified GitHub release assets locally; never contact GitHub.

Run only after building the final installer. The dedicated update channel must
already identify the repository approved for this product.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import struct
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "App Source"
INSTALLER_NAME = "PNG-Mockup-Setup.exe"
MANIFEST_NAME = "png-mockup-update.json"
FOOTER = struct.Struct("<q32s8s")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("Use the exact owner/repository for PNG Mockup.")
    if value.casefold() == "minhtuan5991/autodesignpod":
        raise ValueError("Never use the original application's release repository.")
    return value


def inspect_installer(installer: Path, repository: str, app_root: Path = APP) -> dict:
    repository = validate_repository(repository)
    identity = json.loads((app_root / "version.json").read_text(encoding="utf-8-sig"))
    if identity.get("product_id") != "png-mockup" or identity.get("name") != "PNG Mockup":
        raise ValueError("Incorrect product identity.")
    version = identity.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Release version must be a stable major.minor.patch number.")
    channel = json.loads((app_root / "update-channel.json").read_text(encoding="utf-8-sig"))
    expected_channel = {"schema_version": 1, "product_id": "png-mockup", "repository": repository, "enabled": True}
    if channel != expected_channel:
        raise ValueError("The enabled product channel must match the approved release repository.")
    binary = installer.read_bytes()
    if len(binary) <= FOOTER.size or not binary.startswith(b"MZ"):
        raise ValueError("Not a PNG Mockup Windows installer.")
    length, payload_digest, marker = FOOTER.unpack(binary[-FOOTER.size:])
    if marker != b"PNGMSP01" or not 0 < length < len(binary) - FOOTER.size:
        raise ValueError("Installer footer is invalid.")
    payload = binary[-FOOTER.size - length:-FOOTER.size]
    if hashlib.sha256(payload).digest() != payload_digest:
        raise ValueError("Installer payload hash mismatch.")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = json.loads(archive.read("setup_manifest.json"))
        if manifest.get("product_id") != "png-mockup" or manifest.get("version") != version:
            raise ValueError("Installer is stale or belongs to another product.")
        if json.loads(archive.read("version.json")) != identity or json.loads(archive.read("update-channel.json")) != channel:
            raise ValueError("Installer identity/channel does not match the current application.")
        names = archive.namelist()
        records = manifest.get("files", [])
        expected_names = {"setup_manifest.json"}
        for record in records:
            relative = record["path"]
            if not isinstance(relative, str) or "\\" in relative or any(part in {"", ".", ".."} or ":" in part for part in relative.split("/")):
                raise ValueError("Unsafe payload path.")
            if relative in expected_names or relative.split("/")[0] in {"PNG", "Mockup", "Output", "logs", "tests", "build"}:
                raise ValueError("Duplicate payload or private user data included.")
            expected_names.add(relative)
            content = archive.read(relative)
            if len(content) != record["length"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError(f"Payload hash/length mismatch: {relative}")
            source = (app_root / relative).resolve()
            if app_root.resolve() not in source.parents:
                raise ValueError("Source path escaped the application.")
            if relative == "config.json":
                source = app_root.parent / "payload-template" / "config.json"
            if sha256(source) != record["sha256"]:
                raise ValueError(f"Installer no longer matches current source: {relative}")
        if len(names) != len(expected_names) or set(names) != expected_names:
            raise ValueError("Unlisted or duplicate files in payload.")
    digest = hashlib.sha256(binary).hexdigest()
    sidecar = installer.with_name(installer.name + ".sha256")
    if sidecar.read_text(encoding="ascii").split()[0].lower() != digest:
        raise ValueError("Installer checksum sidecar is stale.")
    return {
        "schema_version": 1, "product_id": "png-mockup", "repository": repository,
        "version": version, "tag": f"v{version}",
        "installer": {"name": INSTALLER_NAME, "size": len(binary), "sha256": digest},
    }


def prepare(repository: str) -> Path:
    installer = ROOT / "PNG Mockup Setup.exe"
    manifest = inspect_installer(installer, repository)
    folder = ROOT / "build" / f"release-v{manifest['version']}-{uuid.uuid4().hex}"
    resolved = folder.resolve()
    if (ROOT / "build").resolve() not in resolved.parents:
        raise ValueError("Release staging must remain inside build/.")
    if any(part.is_symlink() or part.is_junction() for part in (folder, *folder.parents)):
        raise ValueError("Release staging must not pass through a junction or symlink.")
    folder.mkdir(parents=True)
    destination = folder / INSTALLER_NAME
    shutil.copy2(installer, destination)
    if sha256(destination) != manifest["installer"]["sha256"]:
        raise ValueError("The release copy failed verification.")
    (folder / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (folder / (INSTALLER_NAME + ".sha256")).write_text(f"{manifest['installer']['sha256']}  {INSTALLER_NAME}\n", encoding="ascii")
    notes_path = ROOT / "releases" / f"{manifest['tag']}.md"
    if not notes_path.is_file():
        raise ValueError(f"Write release notes first: {notes_path}")
    shutil.copy2(notes_path, folder / "release-notes.md")
    print(json.dumps({"prepared_directory": str(folder), **manifest, "published": False}, ensure_ascii=False, indent=2))
    return folder


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    prepare(args.repository)

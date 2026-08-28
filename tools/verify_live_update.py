"""Verify a PUBLIC GitHub update by downloading it, without applying it.

Run only after publication. The official setup is extracted into a new build/
fixture. Its current version must report no update; only that fixture's version
metadata is then changed to 0.0.1 to exercise a real, token-free update download.
No launcher, updater, GUI or Photoshop process is started. Fixtures are retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

# This is a developer-only tool. Importing the packaged backend must not create
# bytecode in the source tree, even if the caller did not use Python's -B flag.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "App Source" / "src"))
from prepare_release import inspect_installer, validate_repository
import update_manager

OLD_VERSION = "0.0.1"
FIXTURE_NAME = "Installed app"


class LiveVerificationError(RuntimeError):
    pass


class Deadline:
    """Cancellation interface consumed by the unmodified update backend."""

    def __init__(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds

    def is_set(self) -> bool:
        return time.monotonic() >= self.until

    def remaining(self) -> float:
        remaining = self.until - time.monotonic()
        if remaining <= 0:
            raise LiveVerificationError("Verification exceeded its time budget.")
        return remaining


def safe_path(path: Path) -> Path:
    absolute = path.absolute()
    for item in (absolute, *absolute.parents):
        try:
            attributes = getattr(item.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if item.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise LiveVerificationError("Junctions/symlinks are not allowed in verification paths.")
    if absolute.resolve() != absolute:
        raise LiveVerificationError("Verification paths must be canonical local paths.")
    return absolute


def sha256(path: Path) -> str:
    with safe_path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(safe_path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise LiveVerificationError("Expected a JSON object: " + str(path))
    return value


def write_json(path: Path, value: dict) -> None:
    path = safe_path(path)
    if path.exists() and path.stat().st_nlink > 1:
        raise LiveVerificationError("A fixture JSON file must not be a shared hard link.")
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Existing installed JSON files are Hidden on Windows. Preserve their flags;
    # opening with wb or replacing the file can fail or remove those attributes.
    with path.open("r+b" if path.exists() else "xb") as output:
        output.write(content)
        output.truncate()
        output.flush()
        os.fsync(output.fileno())


def checked_fixture(fixture: Path) -> Path:
    fixture = safe_path(fixture)
    if (fixture.name != FIXTURE_NAME or fixture.parent.parent != safe_path(ROOT / "build")
            or not fixture.parent.name.startswith("live-update-check-")
            or update_manager._source_checkout(fixture)):
        raise LiveVerificationError("Only this tool's isolated build fixture may be modified.")
    return fixture


def simulate_old_version(fixture: Path) -> list[str]:
    """Change version metadata only; never change channel/config/program bytes."""
    fixture = checked_fixture(fixture)
    identity = read_json(fixture / "version.json")
    manifest = read_json(fixture / "setup_manifest.json")
    if (identity.get("product_id") != "png-mockup" or identity.get("name") != "PNG Mockup"
            or manifest.get("product_id") != "png-mockup"
            or manifest.get("version") != identity.get("version")
            or update_manager._version(identity.get("version")) <= update_manager._version(OLD_VERSION)):
        raise LiveVerificationError("Fixture identity/manifest is not the expected newer PNG Mockup version.")
    records = [item for item in manifest["files"] if item.get("path") == "version.json"]
    if len(records) != 1:
        raise LiveVerificationError("Fixture manifest must own exactly one version.json.")
    receipt_path = fixture / "logs" / ".installation.json"
    receipt = read_json(receipt_path) if receipt_path.exists() else None
    if receipt is not None and (receipt.get("product_id") != "png-mockup"
                                or receipt.get("version") != identity["version"]):
        raise LiveVerificationError("Fixture installation receipt has a different identity/version.")
    identity["version"] = OLD_VERSION
    write_json(fixture / "version.json", identity)
    records[0].update(length=(fixture / "version.json").stat().st_size,
                      sha256=sha256(fixture / "version.json"))
    manifest["version"] = OLD_VERSION
    write_json(fixture / "setup_manifest.json", manifest)
    changed = ["version.json", "setup_manifest.json"]
    if receipt is not None:
        receipt["version"] = OLD_VERSION
        write_json(receipt_path, receipt)
        changed.append("logs/.installation.json")
    return changed


def verify_live(repository: str, installer: Path, *, timeout: int = 300) -> Path:
    if os.name != "nt":
        raise LiveVerificationError("The official headless installer requires Windows.")
    if not 30 <= timeout <= 900:
        raise LiveVerificationError("--timeout must be between 30 and 900 seconds.")
    repository = validate_repository(repository)
    installer = safe_path(installer)
    build = safe_path(ROOT / "build")
    official = installer == safe_path(ROOT / "PNG Mockup Setup.exe")
    staged = (build in installer.parents and installer.name in {"PNG Mockup Setup.exe", "PNG-Mockup-Setup.exe"}
              and not update_manager._source_checkout(installer.parent))
    if not (official or staged):
        # The exact top-level setup is allowed, but executables in App Source
        # and arbitrary source-tree files must never be executed by this tool.
        raise LiveVerificationError("Select the official setup at the project root or inside build/.")
    if not installer.is_file() or not 0 < installer.stat().st_size <= update_manager.MAX_INSTALLER_BYTES:
        raise LiveVerificationError("The official setup is missing or exceeds the safe size limit.")
    safe_path(installer.with_name(installer.name + ".sha256"))
    started = time.monotonic()
    deadline = Deadline(timeout)
    expected = inspect_installer(installer, repository, ROOT / "App Source")
    version, tag = expected["version"], expected["tag"]
    expected_release = update_manager.ReleaseInfo(
        version, tag, "", f"https://github.com/{repository}/releases/tag/{tag}",
        f"https://github.com/{repository}/releases/download/{tag}/PNG-Mockup-Setup.exe",
        expected["installer"]["size"], expected["installer"]["sha256"], repository,
    )
    # Validate the actual PE/footer/ZIP/member hashes and required components
    # before executing this exact official setup in /extract-only mode.
    update_manager.verify_installer(installer, expected_release, cancel_event=deadline)
    deadline.remaining()
    build.mkdir(exist_ok=True)
    work = safe_path(build / ("live-update-check-" + uuid.uuid4().hex))
    work.mkdir()
    fixture = checked_fixture(work / FIXTURE_NAME)
    report_path = work / "live-update-result.json"
    report = {
        "ok": False, "verification_kind": "live GitHub download; simulated old installed version",
        "repository": repository, "expected_version": version, "simulated_version": OLD_VERSION,
        "expected_release_url": expected_release.release_url, "official_installer": str(installer),
        "expected_installer_sha256": expected_release.sha256, "expected_installer_size": expected_release.size,
        "fixture": str(fixture), "report": str(report_path), "live_github_requests": False,
        "live_download_verified": False, "network_mocked": False, "token_used": False,
        "updater_launched": False, "gui_launched": False, "photoshop_launched": False,
        "timeout_seconds": timeout, "socket_timeout_seconds": update_manager.NETWORK_TIMEOUT,
        "backend_log": [], "phase": "extract official setup",
    }
    write_json(report_path, report)

    def log(message: str) -> None:
        report["backend_log"].append({"phase": report["phase"], "message": message})

    try:
        extract_report = work / "extract-result.json"
        process = subprocess.run(
            [str(installer), "/extract", str(fixture), "/report", str(extract_report)],
            cwd=work, timeout=min(120, deadline.remaining()), check=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        extracted = read_json(extract_report)
        if (process.returncode != 0 or extracted.get("ok") is not True
                or extracted.get("product_id") != "png-mockup" or extracted.get("version") != version
                or safe_path(Path(extracted.get("target", ""))) != fixture):
            raise LiveVerificationError("Official /extract failed; inspect extract-result.json in the fixture.")
        channel = update_manager.read_channel(checked_fixture(fixture), log=log)
        if not channel.enabled or channel.repository != repository or update_manager.read_current_version(fixture) != version:
            raise LiveVerificationError("Extracted app has the wrong version/channel or is a source checkout.")
        preserved = {name: sha256(fixture / name) for name in ("config.json", "update-channel.json", "src/update_manager.py")}
        report["phase"] = "check at current installed version"
        report["live_github_requests"] = True
        write_json(report_path, report)
        current = update_manager.check_latest_release(fixture, cancel_event=deadline, log=log)
        deadline.remaining()
        if current is not None or report["backend_log"]:
            raise LiveVerificationError("Current-version check did not complete cleanly with no newer release.")
        report["current_version_reported_newer_release"] = False
        report["simulated_metadata_files"] = simulate_old_version(fixture)
        report["phase"] = "check with simulated 0.0.1 installed version"
        release = update_manager.check_latest_release(fixture, cancel_event=deadline, log=log)
        deadline.remaining()
        if release is None:
            raise LiveVerificationError("No valid public update was received for the simulated old version; see backend_log.")
        if (release.repository != repository or release.version != version or release.tag != tag
                or release.size != expected_release.size or release.sha256.lower() != expected_release.sha256
                or release.release_url != expected_release.release_url or release.download_url != expected_release.download_url):
            raise LiveVerificationError("The live latest release does not match the expected official installer exactly.")
        report.update(release_url=release.release_url, download_url=release.download_url, release_version=release.version,
                      same_version_suppressed=True, phase="download and verify real GitHub asset")
        if (fixture / "logs" / "updates").exists():
            raise LiveVerificationError("A fresh fixture must not contain a cached download.")
        write_json(report_path, report)
        downloaded = update_manager.download_release(
            release, fixture, cancel_event=deadline, log=log,
            progress=lambda received, total: report.update(downloaded_bytes=received, download_total_bytes=total),
        )
        deadline.remaining()
        if downloaded is None:
            raise LiveVerificationError("Live download/installer verification failed; see backend_log.")
        downloaded = safe_path(downloaded)
        digest = sha256(downloaded)
        if fixture / "logs" / "updates" not in downloaded.parents or digest != expected_release.sha256:
            raise LiveVerificationError("Downloaded installer path/hash does not match the official setup.")
        if any(sha256(fixture / name) != digest for name, digest in preserved.items()):
            raise LiveVerificationError("The fixture config, channel or backend unexpectedly changed.")
        report.update(ok=True, phase="complete", downloaded_installer=str(downloaded),
                      downloaded_installer_sha256=digest, live_download_verified=True)
    except Exception as error:
        report["error"] = str(error)
        raise LiveVerificationError(f"Verification failed: {error}\nReport: {report_path}") from None
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(report_path, report)
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="Approved public product repo, e.g. minhtuan5991/PNG-Mockup")
    parser.add_argument("--installer", type=Path, default=ROOT / "PNG Mockup Setup.exe")
    parser.add_argument("--timeout", type=int, default=300, help="Overall budget in seconds (30..900); one in-flight socket read may take 8 more seconds.")
    args = parser.parse_args()
    report = verify_live(args.repository, args.installer, timeout=args.timeout)
    print(report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    try:
        main()
    except (LiveVerificationError, update_manager.UpdateError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None

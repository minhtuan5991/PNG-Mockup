from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import github_release
import prepare_release
import verify_live_update


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="png-release-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "App Source"
        self.app.mkdir()
        (self.root / "payload-template").mkdir()
        self.repository = "png-mockup-tests/product"
        self.identity = {"product_id": "png-mockup", "name": "PNG Mockup", "version": "1.1.0"}
        self.channel = {"schema_version": 1, "product_id": "png-mockup", "repository": self.repository, "enabled": True}
        self.files = {
            "version.json": json.dumps(self.identity).encode(),
            "update-channel.json": json.dumps(self.channel).encode(),
            "config.json": b'{"ui_color_preset":"soft_purple"}',
            "PNG Mockup.exe": b"fixture-program-not-executed",
        }
        for name, content in self.files.items():
            (self.app / name).write_bytes(content)
        (self.root / "payload-template" / "config.json").write_bytes(self.files["config.json"])
        # This private preference must never be uploaded in a release.
        (self.app / "config.json").write_text('{"ui_color_preset":"mint","private_note":"keep local"}')
        self.installer = self.root / "PNG Mockup Setup.exe"
        self.write_installer()

    def write_installer(self, *, additional: dict[str, bytes] | None = None, version: str = "1.1.0") -> None:
        files = self.files | (additional or {})
        manifest = {"format": 1, "product_id": "png-mockup", "version": version, "files": []}
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
                manifest["files"].append({"path": name, "length": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            archive.writestr("setup_manifest.json", json.dumps(manifest))
        body = payload.getvalue()
        binary = b"MZ" + b"fixture-stub-never-executed" + body + struct.pack("<q32s8s", len(body), hashlib.sha256(body).digest(), b"PNGMSP01")
        self.installer.write_bytes(binary)
        self.installer.with_name(self.installer.name + ".sha256").write_text(hashlib.sha256(binary).hexdigest() + "  PNG Mockup Setup.exe\n")

    def inspect(self) -> dict:
        return prepare_release.inspect_installer(self.installer, self.repository, self.app)

    def test_release_asset_uses_factory_config_and_exact_payload_hash(self) -> None:
        result = self.inspect()
        self.assertEqual(result["installer"]["name"], "PNG-Mockup-Setup.exe")
        self.assertEqual(result["tag"], "v1.1.0")
        self.assertEqual(result["installer"]["sha256"], hashlib.sha256(self.installer.read_bytes()).hexdigest())
        self.assertIn("private_note", (self.app / "config.json").read_text())

    def test_stale_installer_and_changed_source_cannot_be_prepared(self) -> None:
        self.write_installer(version="1.0.0")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.inspect()
        self.write_installer()
        (self.app / "PNG Mockup.exe").write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "current source"):
            self.inspect()

    def test_disabled_or_different_channel_is_not_releasable(self) -> None:
        for change in ({"enabled": False}, {"repository": "another/product"}, {"product_id": "foreign"}):
            with self.subTest(change=change):
                (self.app / "update-channel.json").write_text(json.dumps(self.channel | change))
                with self.assertRaisesRegex(ValueError, "enabled product channel"):
                    self.inspect()

    def test_user_data_in_payload_is_rejected(self) -> None:
        self.write_installer(additional={"PNG/private.png": b"user image"})
        with self.assertRaisesRegex(ValueError, "private user data"):
            self.inspect()

    def test_payload_and_sidecar_tampering_are_rejected(self) -> None:
        binary = bytearray(self.installer.read_bytes())
        binary[100] ^= 1
        self.installer.write_bytes(binary)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.inspect()
        self.write_installer()
        self.installer.with_name(self.installer.name + ".sha256").write_text("0" * 64 + "  PNG Mockup Setup.exe\n")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            self.inspect()

    def test_original_repository_and_urls_are_never_accepted(self) -> None:
        for invalid in ("minhtuan5991/AutoDesignPOD", "https://github.com/owner/repo", "owner/repo/extra", "owner/repo?secret=1"):
            with self.subTest(repository=invalid), self.assertRaises(ValueError):
                prepare_release.validate_repository(invalid)


class PublishingSafetyTests(unittest.TestCase):
    def test_credential_cannot_be_sent_to_other_hosts(self) -> None:
        client = object.__new__(github_release.GitHub)
        client._token = "test-token-not-a-real-credential"
        client._opener = MagicMock()
        for url in ("https://example.com/", "http://api.github.com/user", "https://api.github.com.evil.test/user", "https://token@api.github.com/user"):
            with self.subTest(url=url), self.assertRaises(github_release.ReleaseError):
                client._request("GET", url)
        client._opener.open.assert_not_called()

    def test_existing_repo_visibility_is_never_changed_implicitly(self) -> None:
        client = MagicMock()
        client.account = "owner"
        client.json.return_value = {"full_name": "owner/product", "private": True, "html_url": "https://github.com/owner/product", "permissions": {"push": True}}
        with self.assertRaisesRegex(github_release.ReleaseError, "visibility differs"):
            github_release.create_repo(client, "owner/product", "public")
        client.json.assert_called_once_with("GET", "/repos/owner/product", allow_missing=True)


@unittest.skipUnless(os.name == "nt", "The development live-check tool uses the Windows installer.")
class LiveUpdateToolSafetyTests(unittest.TestCase):
    """Offline orchestration tests: no real HTTP, EXE, app or updater execution."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="png-live-tool-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "App Source").mkdir()
        (self.root / "build").mkdir()
        (self.root / "build_setup.ps1").write_text("# unit fixture only\n")
        self.repository = "png-mockup-tests/product"
        self.identity = {"product_id": "png-mockup", "name": "PNG Mockup", "version": "1.1.0"}
        self.channel = {"schema_version": 1, "product_id": "png-mockup", "repository": self.repository, "enabled": True}
        self.bytes = b"MZ-unit-fixture-never-executed"
        self.installer = self.root / "PNG Mockup Setup.exe"
        self.installer.write_bytes(self.bytes)
        self.installer.with_name(self.installer.name + ".sha256").write_text(hashlib.sha256(self.bytes).hexdigest())
        self.release = verify_live_update.update_manager.ReleaseInfo(
            "1.1.0", "v1.1.0", "unit fixture", f"https://github.com/{self.repository}/releases/tag/v1.1.0",
            f"https://github.com/{self.repository}/releases/download/v1.1.0/PNG-Mockup-Setup.exe",
            len(self.bytes), hashlib.sha256(self.bytes).hexdigest(), self.repository,
        )
        self.expected = {
            "version": self.release.version, "tag": self.release.tag,
            "installer": {"size": self.release.size, "sha256": self.release.sha256},
        }
        self.patch_value(verify_live_update, "ROOT", self.root)
        blocked_network = patch("socket.socket", side_effect=AssertionError("Unit tests must not use real HTTP."))
        blocked_network.start()
        self.addCleanup(blocked_network.stop)
        self.inspector = self.patch_value(verify_live_update, "inspect_installer", MagicMock(return_value=self.expected))
        self.verifier = self.patch_value(verify_live_update.update_manager, "verify_installer", MagicMock())
        self.extractor = self.patch_value(verify_live_update.subprocess, "run", MagicMock(side_effect=self.extract))
        self.checked_versions = []
        self.checker = self.patch_value(verify_live_update.update_manager, "check_latest_release", MagicMock(side_effect=self.check))
        self.downloader = self.patch_value(verify_live_update.update_manager, "download_release", MagicMock(side_effect=self.download))
        self.launcher = self.patch_value(verify_live_update.update_manager, "launch_updater", MagicMock(side_effect=AssertionError("Do not launch updates.")))

    def patch_value(self, owner, name, value):
        active = patch.object(owner, name, value)
        active.start()
        self.addCleanup(active.stop)
        return value

    def install_files(self, target: Path) -> None:
        files = {
            "version.json": json.dumps(self.identity).encode(),
            "update-channel.json": json.dumps(self.channel).encode(),
            "config.json": b'{"user_setting":"keep"}',
            "src/update_manager.py": b"# never executed fixture backend\n",
        }
        records = []
        for name, content in files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            records.append({"path": name, "length": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        (target / "setup_manifest.json").write_text(json.dumps({
            "format": 1, "product_id": "png-mockup", "version": self.identity["version"], "files": records,
        }))
        (target / "logs").mkdir()
        (target / "logs" / ".installation.json").write_text(json.dumps(self.identity | {"state": "installed"}))

    def extract(self, command, **options):
        self.assertEqual(command[0:2], [str(self.installer), "/extract"])
        self.assertEqual(command[3], "/report")
        target = Path(command[2])
        self.install_files(target)
        Path(command[4]).write_text(json.dumps({
            "ok": True, "product_id": "png-mockup", "version": "1.1.0", "target": str(target),
        }))
        return SimpleNamespace(returncode=0)

    def check(self, target, **options):
        self.checked_versions.append(verify_live_update.update_manager.read_current_version(target))
        return None if len(self.checked_versions) == 1 else self.release

    def download(self, release, target, **options):
        destination = target / "logs" / "updates" / "unit-fixture" / "PNG Mockup Setup.exe"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(self.bytes)
        options["progress"](len(self.bytes), len(self.bytes))
        return destination

    def run_tool(self) -> Path:
        return verify_live_update.verify_live(self.repository, self.installer)

    def test_orchestration_checks_current_then_simulated_old_and_only_downloads(self) -> None:
        report_path = self.run_tool()
        result = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(result["ok"] and result["same_version_suppressed"])
        self.assertEqual(self.checked_versions, ["1.1.0", "0.0.1"])
        self.assertEqual(result["downloaded_installer_sha256"], self.release.sha256)
        self.assertEqual(result["release_url"], self.release.release_url)
        self.assertEqual(Path(result["fixture"]).parent, report_path.parent)
        self.assertFalse(result["updater_launched"] or result["gui_launched"] or result["photoshop_launched"])
        self.assertEqual(self.installer.read_bytes(), self.bytes)
        self.extractor.assert_called_once()
        self.launcher.assert_not_called()

    def test_offline_is_a_failed_verification_not_a_same_version_success(self) -> None:
        def offline(target, **options):
            options["log"]("Background update check skipped (URLError).")
            return None
        self.checker.side_effect = offline
        with self.assertRaisesRegex(verify_live_update.LiveVerificationError, "did not complete cleanly"):
            self.run_tool()
        self.checker.assert_called_once()
        self.downloader.assert_not_called()
        result = json.loads(next((self.root / "build").glob("*/live-update-result.json")).read_text())
        self.assertFalse(result["ok"] or result["live_download_verified"])

    def test_wrong_published_hash_or_existing_cache_prevents_download(self) -> None:
        from dataclasses import replace
        self.checker.side_effect = [None, replace(self.release, sha256="f" * 64)]
        with self.assertRaisesRegex(verify_live_update.LiveVerificationError, "does not match"):
            self.run_tool()
        self.downloader.assert_not_called()
        self.checker.side_effect = [None, self.release]
        def cached_extract(command, **options):
            result = self.extract(command, **options)
            (Path(command[2]) / "logs" / "updates").mkdir()
            return result
        self.extractor.side_effect = cached_extract
        with self.assertRaisesRegex(verify_live_update.LiveVerificationError, "must not contain a cached download"):
            self.run_tool()
        self.downloader.assert_not_called()

    def test_source_checkout_or_external_installer_is_rejected_before_execution(self) -> None:
        for path in (self.root / "App Source" / "PNG Mockup Setup.exe", self.root / "foreign.exe"):
            with self.subTest(path=path), self.assertRaisesRegex(verify_live_update.LiveVerificationError, "official setup"):
                verify_live_update.verify_live(self.repository, path)
        for target in (self.root, self.root / "App Source", self.root / "build" / "arbitrary" / "Installed app"):
            with self.subTest(target=target), self.assertRaisesRegex(verify_live_update.LiveVerificationError, "isolated build fixture"):
                verify_live_update.simulate_old_version(target)
        self.inspector.assert_not_called()
        self.extractor.assert_not_called()
        self.checker.assert_not_called()

    def test_simulated_version_preserves_hidden_attributes_and_nonmetadata(self) -> None:
        from ctypes import windll
        target = self.root / "build" / "live-update-check-hidden-unit" / "Installed app"
        self.install_files(target)
        version = target / "version.json"
        before = {name: (target / name).read_bytes() for name in ("config.json", "update-channel.json", "src/update_manager.py")}
        attributes = version.stat().st_file_attributes | stat.FILE_ATTRIBUTE_HIDDEN
        self.assertTrue(windll.kernel32.SetFileAttributesW(str(version), attributes))
        changed = verify_live_update.simulate_old_version(target)
        self.assertEqual(version.stat().st_file_attributes, attributes)
        self.assertEqual(verify_live_update.read_json(version)["version"], "0.0.1")
        for name, content in before.items():
            self.assertEqual((target / name).read_bytes(), content)
        manifest = verify_live_update.read_json(target / "setup_manifest.json")
        record = next(item for item in manifest["files"] if item["path"] == "version.json")
        self.assertEqual(record["sha256"], hashlib.sha256(version.read_bytes()).hexdigest())
        self.assertIn("logs/.installation.json", changed)

    def test_deadline_and_timeout_are_bounded(self) -> None:
        expired = verify_live_update.Deadline(0)
        self.assertTrue(expired.is_set())
        with self.assertRaisesRegex(verify_live_update.LiveVerificationError, "time budget"):
            expired.remaining()
        for timeout in (0, 901):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(verify_live_update.LiveVerificationError, "30 and 900"):
                verify_live_update.verify_live(self.repository, self.installer, timeout=timeout)
        self.extractor.assert_not_called()
        self.checker.assert_not_called()


if __name__ == "__main__":
    unittest.main()

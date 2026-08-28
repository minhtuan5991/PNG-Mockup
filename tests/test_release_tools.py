from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import MagicMock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import github_release
import prepare_release


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


if __name__ == "__main__":
    unittest.main()

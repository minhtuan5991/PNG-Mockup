"""Serverless update tests: no network, real installer execution, or GUI."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request
import zipfile

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))
import update_manager as updates

REPOSITORY = "example/png-mockup"
VERSION = "1.1.0"
TAG = "v" + VERSION


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def channel(repository=REPOSITORY, enabled=True):
    return {"schema_version": 1, "product_id": "png-mockup", "repository": repository, "enabled": enabled}


def package(*, version=VERSION, repository=REPOSITORY, change_files=None, change_manifest=None, extra=None):
    files = {name: b"test fixture" for name in (
        "PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe",
        "config.json", "src/gui.py", "src/app.py", "src/update_manager.py",
        "assets/app_icon.ico", "runtime/python/python.exe", "runtime/python/pythonw.exe",
        "runtime/python/python312.dll", "runtime/python/python312._pth",
        "runtime/python/Lib/tkinter/__init__.py", "runtime/python/Lib/site-packages/PIL/__init__.py",
        "runtime/python/tcl/tcl8.6/init.tcl", "runtime/python/tcl/tk8.6/tk.tcl",
        "tools/runtime_check.py", "tools/oxipng/oxipng.exe", "runtime/python/LICENSE.txt",
        "tools/oxipng/LICENSE", "THIRD_PARTY_NOTICES.md",
    )}
    files["version.json"] = encode({"product_id": "png-mockup", "name": "PNG Mockup", "version": version})
    files["update-channel.json"] = encode(channel(repository))
    if change_files:
        change_files(files)
    manifest = {
        "format": 1, "product_id": "png-mockup", "version": version,
        "files": [{"path": name, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()],
    }
    if change_manifest:
        change_manifest(manifest)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("setup_manifest.json", encode(manifest))
        if extra:
            extra(archive)
    payload = stream.getvalue()
    header = bytearray(128)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    header[64:68] = b"PE\0\0"
    return bytes(header) + payload + struct.pack("<q32s8s", len(payload), hashlib.sha256(payload).digest(), b"PNGMSP01")


def release_for(data, *, version=VERSION, repository=REPOSITORY):
    tag = "v" + version
    return updates.ReleaseInfo(
        version, tag, "Release notes",
        f"https://github.com/{repository}/releases/tag/{tag}",
        f"https://github.com/{repository}/releases/download/{tag}/PNG-Mockup-Setup.exe",
        len(data), hashlib.sha256(data).hexdigest(), repository,
    )


def release_documents(release):
    manifest = {
        "schema_version": 1, "product_id": "png-mockup", "repository": release.repository,
        "version": release.version, "tag": release.tag,
        "installer": {"name": "PNG-Mockup-Setup.exe", "size": release.size, "sha256": release.sha256},
    }
    metadata = {
        "tag_name": release.tag, "draft": False, "prerelease": False, "body": release.notes,
        "assets": [
            {"name": "png-mockup-update.json", "state": "uploaded", "size": len(encode(manifest)),
             "browser_download_url": f"https://github.com/{release.repository}/releases/download/{release.tag}/png-mockup-update.json"},
            {"name": "PNG-Mockup-Setup.exe", "state": "uploaded", "size": release.size,
             "digest": "sha256:" + release.sha256, "browser_download_url": release.download_url},
        ],
    }
    return metadata, manifest


class Response(io.BytesIO):
    def __init__(self, data, url, headers=None, code=200):
        super().__init__(data)
        self.url, self.code = url, code
        self.headers = headers or {}

    def geturl(self):
        return self.url

    def getcode(self):
        return self.code


class UpdateFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "PNG Mockup độc lập"
        self.root.mkdir()
        self.write_channel(channel())
        (self.root / "version.json").write_bytes(encode({"product_id": "png-mockup", "name": "PNG Mockup", "version": "1.0.0"}))
        self.data = package()
        self.release = release_for(self.data)
        self.messages = []
        network = patch("socket.socket", side_effect=AssertionError("Unit tests must never access a network"))
        network.start()
        self.addCleanup(network.stop)

    def write_channel(self, value):
        (self.root / "update-channel.json").write_bytes(encode(value))

    def api_responses(self, metadata=None, manifest=None):
        if metadata is None:
            metadata, manifest = release_documents(self.release)
        if manifest is not None:
            metadata["assets"][0]["size"] = len(encode(manifest))
        responses = [
            Response(encode(metadata), f"https://api.github.com/repos/{REPOSITORY}/releases/latest"),
            Response(encode(manifest), f"https://github.com/{REPOSITORY}/releases/download/{metadata['tag_name']}/png-mockup-update.json"),
        ]
        return SimpleNamespace(open=MagicMock(side_effect=responses))

    def download(self, data=None, release=None, **kwargs):
        release = release or self.release
        data = self.data if data is None else data
        response = Response(data, release.download_url, {"Content-Length": str(len(data))})
        opener = SimpleNamespace(open=MagicMock(return_value=response))
        with patch.object(updates.request, "build_opener", return_value=opener):
            result = updates.download_release(release, self.root, log=self.messages.append, **kwargs)
        return result, opener


class ChannelAndVersionTests(UpdateFixture):
    def test_every_start_checks_enabled_channel_regardless_of_old_24_hour_setting(self):
        (self.root / "config.json").write_bytes(encode({
            "update_check_on_start": False, "update_check_interval_hours": 720,
            "update_github_repo": "minhtuan5991/AutoDesignPOD",
        }))
        for interval in (0, 1, 24, 720):
            self.assertTrue(updates.auto_check_due(self.root, interval))
            self.assertTrue(updates.auto_check_due(self.root, interval))
        self.assertEqual(updates.read_channel(self.root).repository, REPOSITORY)

    def test_missing_disabled_invalid_and_legacy_channels_never_call_network(self):
        values = [channel(enabled=False), channel("", False), channel("", True),
                  channel("minhtuan5991/AutoDesignPOD"), channel("https://github.com/example/png-mockup"),
                  dict(channel(), product_id="auto-mockup"), dict(channel(), schema_version=True),
                  dict(channel(), enabled="true"), [], None]
        for value in values:
            with self.subTest(value=value), patch.object(updates.request, "build_opener") as opener:
                self.write_channel(value)
                self.assertFalse(updates.read_channel(self.root).enabled)
                self.assertIsNone(updates.check_latest_release(self.root, log=self.messages.append))
                opener.assert_not_called()
        (self.root / "update-channel.json").unlink()
        self.assertFalse(updates.auto_check_due(self.root, 24))

    def test_stable_version_comparison_handles_patch_minor_and_future_versions(self):
        for remote, expected in (("0.9.9", False), ("1.0.0", False), ("1.0.1", True),
                                 ("1.10.0", True), ("2.0.0", True), ("999.0.0", True)):
            with self.subTest(remote=remote):
                release = release_for(self.data, version=remote)
                metadata, manifest = release_documents(release)
                opener = self.api_responses(metadata, manifest)
                with patch.object(updates.request, "build_opener", return_value=opener):
                    result = updates.check_latest_release(self.root)
                self.assertEqual(result is not None, expected)
                self.assertEqual(opener.open.call_count, 2 if expected else 1)

    def test_invalid_versions_never_become_updates(self):
        for tag in ("1", "v1.2", "v01.2.3", "v1.2.3-beta", "v1.2.3+meta", "v-1.2.3",
                    "v999999999999.0.0", "v1.2.3/../../x", None, True):
            with self.subTest(tag=tag):
                metadata, manifest = release_documents(self.release)
                metadata["tag_name"] = tag
                opener = self.api_responses(metadata, manifest)
                with patch.object(updates.request, "build_opener", return_value=opener):
                    self.assertIsNone(updates.check_latest_release(self.root))
                self.assertEqual(opener.open.call_count, 1)

    def test_wrong_installed_product_cannot_request_updates(self):
        (self.root / "version.json").write_bytes(encode({"product_id": "other", "name": "PNG Mockup", "version": "1.0.0"}))
        with patch.object(updates.request, "build_opener") as opener:
            self.assertIsNone(updates.check_latest_release(self.root))
        opener.assert_not_called()

    def test_deeply_nested_channel_is_disabled_without_startup_error(self):
        (self.root / "update-channel.json").write_bytes(b'{"nested":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}")
        self.assertFalse(updates.read_channel(self.root, log=self.messages.append).enabled)
        self.assertTrue(self.messages)

    def test_source_checkout_cannot_offer_updates_but_installed_build_fixtures_can(self):
        project = self.root / "development-project"
        source = project / "App Source"
        source.mkdir(parents=True)
        (project / "build_setup.ps1").write_text("# source marker", encoding="utf-8")
        encoded_channel = encode(channel())
        for root in (project, source, source / "nested", project / "build" / "installed-fixture"):
            root.mkdir(parents=True, exist_ok=True)
            (root / "update-channel.json").write_bytes(encoded_channel)
            (root / "version.json").write_bytes(encode({"product_id": "png-mockup", "name": "PNG Mockup", "version": "1.0.0"}))
            expected_enabled = root == project / "build" / "installed-fixture"
            self.assertEqual(updates.read_channel(root).enabled, expected_enabled, root)
            self.assertEqual(updates.auto_check_due(root, 24), expected_enabled, root)
            self.assertEqual((root / "update-channel.json").read_bytes(), encoded_channel)
            if not expected_enabled:
                self.assertIn("mã nguồn", updates.read_channel(root).reason)
                with patch.object(updates.request, "build_opener") as opener:
                    self.assertIsNone(updates.check_latest_release(root))
                opener.assert_not_called()


class ReleaseCheckTests(UpdateFixture):
    def test_matching_public_latest_release_returns_only_pinned_installer(self):
        opener = self.api_responses()
        with patch.object(updates.request, "build_opener", return_value=opener):
            result = updates.check_latest_release(self.root)
        self.assertEqual(result, self.release)
        for call in opener.open.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 8)
            self.assertNotIn("Authorization", call.args[0].headers)
        self.assertEqual(opener.open.call_args_list[0].args[0].full_url,
                         f"https://api.github.com/repos/{REPOSITORY}/releases/latest")

    def test_offline_404_and_rate_limit_are_logged_without_raising(self):
        failures = [URLError("offline secret"), TimeoutError("slow"),
                    HTTPError("https://api.github.com", 404, "not found", {}, None),
                    HTTPError("https://api.github.com", 403, "rate limited", {}, None)]
        for failure in failures:
            opener = SimpleNamespace(open=MagicMock(side_effect=failure))
            with self.subTest(failure=failure), patch.object(updates.request, "build_opener", return_value=opener):
                self.assertIsNone(updates.check_latest_release(self.root, log=self.messages.append))
        self.assertEqual(len(self.messages), 4)
        self.assertTrue(all("secret" not in message for message in self.messages))

    def test_draft_and_prerelease_are_rejected(self):
        for field in ("draft", "prerelease"):
            metadata, manifest = release_documents(self.release)
            metadata[field] = True
            opener = self.api_responses(metadata, manifest)
            with patch.object(updates.request, "build_opener", return_value=opener):
                self.assertIsNone(updates.check_latest_release(self.root))
            self.assertEqual(opener.open.call_count, 1)

    def test_manifest_product_repository_version_tag_size_hash_and_asset_name_are_required(self):
        changes = (
            lambda value: value.update(product_id="another-app"),
            lambda value: value.update(repository="example/another-app"),
            lambda value: value.update(version="1.2.0"),
            lambda value: value.update(tag="v1.2.0"),
            lambda value: value.update(schema_version=2),
            lambda value: value["installer"].update(name="../unsafe.exe"),
            lambda value: value["installer"].update(name="PNG Mockup Setup.exe"),
            lambda value: value["installer"].update(size=0),
            lambda value: value["installer"].update(size=True),
            lambda value: value["installer"].update(size=self.release.size + 1),
            lambda value: value["installer"].update(sha256="0" * 64),
            lambda value: value["installer"].update(sha256="bad"),
        )
        for change in changes:
            metadata, manifest = release_documents(self.release)
            change(manifest)
            opener = self.api_responses(metadata, manifest)
            with self.subTest(change=change), patch.object(updates.request, "build_opener", return_value=opener):
                self.assertIsNone(updates.check_latest_release(self.root))

    def test_foreign_manifest_asset_url_is_rejected_before_second_request(self):
        for url in ("http://github.com/example/png-mockup/releases/download/v1.1.0/png-mockup-update.json",
                    "https://github.com/example/other/releases/download/v1.1.0/png-mockup-update.json",
                    "https://github.com@example.invalid/manifest", "file:///C:/manifest"):
            metadata, manifest = release_documents(self.release)
            metadata["assets"][0]["browser_download_url"] = url
            opener = self.api_responses(metadata, manifest)
            with self.subTest(url=url), patch.object(updates.request, "build_opener", return_value=opener):
                self.assertIsNone(updates.check_latest_release(self.root))
            self.assertEqual(opener.open.call_count, 1)

    def test_wrong_installer_url_state_or_duplicate_asset_is_rejected(self):
        changes = (
            lambda data: data["assets"][1].update(browser_download_url=self.release.download_url.replace(REPOSITORY, "example/other")),
            lambda data: data["assets"][1].update(browser_download_url=self.release.download_url.replace(TAG, "v9.0.0")),
            lambda data: data["assets"][1].update(state="starter"),
            lambda data: data["assets"].append(copy.deepcopy(data["assets"][1])),
        )
        for change in changes:
            metadata, manifest = release_documents(self.release)
            change(metadata)
            opener = self.api_responses(metadata, manifest)
            with self.subTest(change=change), patch.object(updates.request, "build_opener", return_value=opener):
                self.assertIsNone(updates.check_latest_release(self.root))

    def test_cancelled_check_does_not_open_any_connection(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(updates.request, "build_opener") as opener:
            self.assertIsNone(updates.check_latest_release(self.root, cancel_event=cancel))
        opener.assert_not_called()


class RedirectTests(unittest.TestCase):
    def test_release_asset_redirects_only_to_exact_github_cdn_hosts(self):
        validate = updates._asset_validator(REPOSITORY, TAG, "PNG-Mockup-Setup.exe", redirects=True)
        handler = updates._PinnedRedirectHandler(validate)
        request = Request(f"https://github.com/{REPOSITORY}/releases/download/{TAG}/PNG-Mockup-Setup.exe")
        for host in ("release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"):
            url = f"https://{host}/github-production-release-asset/1/file?signature=test"
            with self.subTest(host=host):
                self.assertEqual(handler.redirect_request(request, None, 302, "", {}, url).full_url, url)
        blocked = (
            "http://release-assets.githubusercontent.com/file",
            "https://release-assets.githubusercontent.com.evil.invalid/file",
            "https://raw.githubusercontent.com/example/repo/main/file",
            "https://github.com/example/other/releases/download/v1.1.0/PNG-Mockup-Setup.exe",
            "https://user:password@objects.githubusercontent.com/file",
            "https://objects.githubusercontent.com:8443/file",
            "https://objects.githubusercontent.com:not-a-port/file",
            "file:///C:/fake.exe", "https://example.invalid/update.exe",
        )
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(updates.UpdateError):
                handler.redirect_request(request, None, 302, "", {}, url)

    def test_api_redirect_cannot_switch_repository_or_leave_api(self):
        handler = updates._PinnedRedirectHandler(updates._api_validator(REPOSITORY))
        request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest")
        for url in ("https://api.github.com/repos/example/other/releases/latest",
                    "https://api.github.com/repositories/123/releases/latest",
                    "https://github.com/example/png-mockup", "http://api.github.com/repos/example/png-mockup/releases/latest"):
            with self.subTest(url=url), self.assertRaises(updates.UpdateError):
                handler.redirect_request(request, None, 301, "", {}, url)

    def test_final_response_url_is_rechecked_even_without_handler_callback(self):
        response = Response(b"{}", "https://example.invalid/stolen")
        opener = SimpleNamespace(open=MagicMock(return_value=response))
        with patch.object(updates.request, "build_opener", return_value=opener):
            with self.assertRaises(updates.UpdateError):
                updates._open_url(f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                                  updates._api_validator(REPOSITORY), accept="application/json")
        self.assertTrue(response.closed)

    def test_cancel_before_redirect_stops_next_request(self):
        cancel = threading.Event()
        cancel.set()
        handler = updates._PinnedRedirectHandler(updates._api_validator(REPOSITORY), cancel)
        url = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
        with self.assertRaises(updates.UpdateCancelled):
            handler.redirect_request(Request(url), None, 302, "", {}, url)


class DownloadTests(UpdateFixture):
    def test_download_verifies_and_atomically_publishes_private_cache_without_execution(self):
        progress = []
        preserved = {}
        for name in ("PNG/design.png", "Mockup/mockup.psd", "Output/export.png", "logs/queue.json"):
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"do not change")
            preserved[path] = path.read_bytes()
        with patch.object(updates.subprocess, "Popen") as popen:
            result, opener = self.download(progress=lambda count, total: progress.append((count, total)))
        self.assertIsNotNone(result, self.messages)
        self.assertEqual(result.read_bytes(), self.data)
        self.assertTrue(result.is_relative_to(self.root / "logs" / "updates"))
        self.assertEqual(progress[-1], (len(self.data), len(self.data)))
        self.assertFalse(list(result.parent.glob("*.part")))
        self.assertEqual(json.loads((result.parent / "release.json").read_text())["product_id"], "png-mockup")
        self.assertTrue(all(path.read_bytes() == data for path, data in preserved.items()))
        popen.assert_not_called()
        opener.open.assert_called_once()

    def test_valid_cache_is_reused_only_after_reverification(self):
        path, _ = self.download()
        with patch.object(updates.request, "build_opener") as opener:
            result = updates.download_release(self.release, self.root)
        self.assertEqual(result, path)
        opener.assert_not_called()
        path.write_bytes(b"corrupted cache")
        repaired, opener = self.download()
        self.assertEqual(repaired, path)
        self.assertEqual(path.read_bytes(), self.data)
        opener.open.assert_called_once()

    def test_content_length_wrong_hash_and_short_or_overlong_stream_are_rejected(self):
        cases = (
            (self.data, {"Content-Length": str(len(self.data) + 1)}, self.release),
            (self.data, {}, replace(self.release, sha256="0" * 64)),
            (self.data[:-1], {}, self.release),
            (self.data + b"extra", {}, self.release),
        )
        for data, headers, release in cases:
            response = Response(data, release.download_url, headers)
            opener = SimpleNamespace(open=MagicMock(return_value=response))
            with self.subTest(size=len(data), hash=release.sha256), patch.object(updates.request, "build_opener", return_value=opener):
                self.assertIsNone(updates.download_release(release, self.root))
            cache = self.root / "logs" / "updates"
            self.assertFalse(list(cache.rglob("*.part")))
            self.assertFalse(list(cache.rglob("*.exe")))

    def test_cancel_after_first_chunk_removes_partial_without_publishing(self):
        cancel = threading.Event()
        def progress(count, total):
            cancel.set()
        result, _ = self.download(progress=progress, cancel_event=cancel)
        self.assertIsNone(result)
        cache = self.root / "logs" / "updates"
        self.assertFalse(list(cache.rglob("*.part")))
        self.assertFalse(list(cache.rglob("*.exe")))
        self.assertTrue(any("UpdateCancelled" in message for message in self.messages))

    def test_offline_download_does_not_raise_or_leave_partial(self):
        opener = SimpleNamespace(open=MagicMock(side_effect=URLError("offline")))
        with patch.object(updates.request, "build_opener", return_value=opener):
            self.assertIsNone(updates.download_release(self.release, self.root, log=self.messages.append))
        self.assertFalse(list((self.root / "logs" / "updates").rglob("*.part")))

    def test_disabled_or_other_repo_release_never_downloads(self):
        for candidate in (replace(self.release, repository="other/product"),
                          replace(self.release, download_url="https://example.invalid/tool.exe"),
                          replace(self.release, version="1.0.0", tag="v1.0.0")):
            with self.subTest(candidate=candidate), patch.object(updates.request, "build_opener") as opener:
                self.assertIsNone(updates.download_release(candidate, self.root))
                opener.assert_not_called()
        self.write_channel(channel(enabled=False))
        with patch.object(updates.request, "build_opener") as opener:
            self.assertIsNone(updates.download_release(self.release, self.root))
        opener.assert_not_called()

    def test_cache_path_escape_or_reparse_is_rejected(self):
        with self.assertRaises(updates.UpdateError):
            updates._owned_path(self.root, self.root.parent / "outside.exe")
        cache = self.root / "logs" / "updates"
        original = Path.resolve
        def redirected(path, *args, **kwargs):
            if path == cache or cache in path.parents:
                return self.root.parent / "other-app"
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected), patch.object(updates.request, "build_opener") as opener:
            self.assertIsNone(updates.download_release(self.release, self.root))
        opener.assert_not_called()


class PayloadVerificationTests(UpdateFixture):
    def verify(self, data, release=None):
        path = self.root / "fixture-setup.exe"
        path.write_bytes(data)
        updates.verify_installer(path, release or release_for(data))

    def test_valid_product_payload_and_every_member_hash_are_verified(self):
        self.verify(self.data)

    def test_pe_signature_footer_and_payload_hash_are_required(self):
        changes = (
            lambda data: b"XX" + data[2:],
            lambda data: data[:64] + b"XXXX" + data[68:],
            lambda data: data[:-8] + b"OTHERAPP",
            lambda data: data[:-40] + bytes([data[-40] ^ 1]) + data[-39:],
            lambda data: data[:140] + bytes([data[140] ^ 1]) + data[141:],
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(updates.UpdateError):
                self.verify(change(self.data))

    def test_invalid_zip_with_matching_footer_hash_is_rejected(self):
        garbage = b"not-a-zip"
        data = self.data[:128] + garbage + struct.pack("<q32s8s", len(garbage), hashlib.sha256(garbage).digest(), b"PNGMSP01")
        with self.assertRaises((updates.UpdateError, zipfile.BadZipFile)):
            self.verify(data)

    def test_product_version_repo_and_disabled_channel_cannot_change_in_payload(self):
        changes = (
            lambda files: files.update({"version.json": encode({"product_id": "other", "name": "PNG Mockup", "version": VERSION})}),
            lambda files: files.update({"version.json": encode({"product_id": "png-mockup", "name": "Other", "version": VERSION})}),
            lambda files: files.update({"version.json": encode({"product_id": "png-mockup", "name": "PNG Mockup", "version": "9.0.0"})}),
            lambda files: files.update({"update-channel.json": encode(channel("example/other"))}),
            lambda files: files.update({"update-channel.json": encode(channel(enabled=False))}),
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(updates.UpdateError):
                self.verify(package(change_files=change))

    def test_manifest_hash_size_and_declarations_are_verified(self):
        changes = (
            lambda value: value.update(product_id="other"),
            lambda value: value.update(version="9.0.0"),
            lambda value: value["files"][0].update(sha256="0" * 64),
            lambda value: value["files"][0].update(length=123456),
            lambda value: value["files"].pop(),
            lambda value: value["files"].append(dict(value["files"][0])),
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(updates.UpdateError):
                self.verify(package(change_manifest=change))

    def test_zip_path_traversal_user_data_and_unapproved_root_files_are_rejected(self):
        for name in ("../outside.exe", "src/../outside.py", "src\\..\\outside.py", "src/NUL.py",
                     "PNG/design.png", "logs/state.json", "other.exe", "src/file.py:stream", "/src/root.py"):
            with self.subTest(name=name), self.assertRaises(updates.UpdateError):
                self.verify(package(change_files=lambda files: files.update({name: b"unsafe"})))

    def test_missing_required_installed_helpers_and_runtime_are_rejected(self):
        for name in ("Uninstall.exe", "Show technical files.exe", "runtime/python/python312._pth",
                     "update-channel.json", "src/update_manager.py"):
            with self.subTest(name=name), self.assertRaises(updates.UpdateError):
                self.verify(package(change_files=lambda files: files.pop(name)))

    def test_duplicate_case_insensitive_entry_and_symlink_are_rejected(self):
        def add_link(archive):
            info = zipfile.ZipInfo("tools/linked.exe")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"../outside")
        for extra in (lambda archive: archive.writestr("src/GUI.py", b"duplicate"), add_link):
            with self.subTest(extra=extra), self.assertRaises(updates.UpdateError):
                self.verify(package(extra=extra))

    def test_cancel_hash_validation_stops_before_zip_open(self):
        path = self.root / "fixture-setup.exe"
        path.write_bytes(self.data)
        cancel = threading.Event()
        cancel.set()
        with patch.object(updates.zipfile, "ZipFile") as archive:
            with self.assertRaises(updates.UpdateCancelled):
                updates.verify_installer(path, self.release, cancel_event=cancel)
        archive.assert_not_called()

    def test_expanded_member_limit_is_checked_before_member_read(self):
        with patch.object(updates, "MAX_MEMBER_BYTES", 3), self.assertRaises(updates.UpdateError):
            self.verify(self.data)


class LaunchTests(UpdateFixture):
    def test_launch_uses_verified_cache_exact_root_pid_and_relaunch_without_shell(self):
        path, _ = self.download()
        with patch.object(updates.subprocess, "Popen", return_value=object()) as popen:
            result = updates.launch_updater(path, self.root, wait_pid=4321)
        self.assertIs(result, popen.return_value)
        self.assertEqual(popen.call_args.args[0], [
            str(path), "/update", str(self.root.resolve()), "/wait-pid", "4321", "/relaunch",
            "/report", str(path.parent / "install-result.json"),
        ])
        self.assertEqual(popen.call_args.kwargs["cwd"], self.root.resolve())
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertFalse((self.root / "logs" / ".app-session.lock").exists())

    def test_foreign_path_and_tampered_cache_never_execute(self):
        path, _ = self.download()
        foreign = self.root / "unsafe.exe"
        foreign.write_bytes(self.data)
        with patch.object(updates.subprocess, "Popen") as popen:
            with self.assertRaises(updates.UpdateError):
                updates.launch_updater(foreign, self.root, wait_pid=4321)
            path.write_bytes(self.data[:-1] + b"X")
            with self.assertRaises(updates.UpdateError):
                updates.launch_updater(path, self.root, wait_pid=4321)
        popen.assert_not_called()

    def test_install_result_report_cannot_redirect_to_another_app_or_be_a_directory(self):
        path, _ = self.download()
        report = path.parent / "install-result.json"
        original_resolve = Path.resolve
        def redirected(candidate, *args, **kwargs):
            if candidate == report:
                return self.root.parent / "other-app" / "settings.json"
            return original_resolve(candidate, *args, **kwargs)
        with patch.object(updates.subprocess, "Popen") as popen:
            with patch.object(Path, "resolve", redirected):
                with self.assertRaises(updates.UpdateError):
                    updates.launch_updater(path, self.root, wait_pid=4321)
            report.mkdir()
            with self.assertRaises(updates.UpdateError):
                updates.launch_updater(path, self.root, wait_pid=4321)
        popen.assert_not_called()

    def test_channel_change_downgrade_and_bad_pid_never_execute(self):
        path, _ = self.download()
        with patch.object(updates.subprocess, "Popen") as popen:
            for pid in (0, -1, True, 2**32, "4321"):
                with self.subTest(pid=pid), self.assertRaises(updates.UpdateError):
                    updates.launch_updater(path, self.root, wait_pid=pid)
            self.write_channel(channel("example/other"))
            with self.assertRaises(updates.UpdateError):
                updates.launch_updater(path, self.root, wait_pid=4321)
            self.write_channel(channel())
            (self.root / "version.json").write_bytes(encode({"product_id": "png-mockup", "name": "PNG Mockup", "version": VERSION}))
            with self.assertRaises(updates.UpdateError):
                updates.launch_updater(path, self.root, wait_pid=4321)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

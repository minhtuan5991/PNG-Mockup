"""Actual backend -> C# installer update, with a simulated endpoint/old version.

This is NOT a live GitHub download. The only HTTP operation is mocked with bytes
from an explicitly labelled test fixture, retaining the real built installer
stub, application source, helpers and private runtime. No GUI or Photoshop runs.
All fixtures, installed files and reports stay in a unique build/ subdirectory
and are retained for inspection. App Source and its real channel are never edited.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
import winreg
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))
import update_manager

FOOTER = struct.Struct("<q32s8s")
TEST_REPOSITORY = "png-mockup-tests/isolated-fixture"
OLD_VERSION = "0.0.1"


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class FixtureResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


@unittest.skipUnless(os.name == "nt", "Actual PNG Mockup installer integration requires Windows.")
class UpdateInstallFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        selected = os.environ.get("PNG_MOCKUP_TEST_SETUP")
        cls.setup = Path(selected).resolve() if selected else ROOT / "PNG Mockup Setup.exe"
        if not cls.setup.is_file():
            raise unittest.SkipTest("Build a current setup and set PNG_MOCKUP_TEST_SETUP for the actual update fixture test.")
        build_root = (ROOT / "build").resolve()
        if cls.setup != ROOT / "PNG Mockup Setup.exe" and build_root not in cls.setup.parents:
            raise AssertionError("Only installers built inside this project may be executed by this test.")
        cls.binary = cls.setup.read_bytes()
        if len(cls.binary) < FOOTER.size:
            raise AssertionError("The selected setup has no PNG Mockup payload.")
        length, digest, marker = FOOTER.unpack(cls.binary[-FOOTER.size:])
        if marker != b"PNGMSP01" or not 0 < length < len(cls.binary) - FOOTER.size:
            raise AssertionError("The selected setup has an invalid PNG Mockup footer.")
        cls.payload = cls.binary[-FOOTER.size - length:-FOOTER.size]
        cls.stub = cls.binary[:-FOOTER.size - length]
        if hashlib.sha256(cls.payload).digest() != digest:
            raise AssertionError("The selected setup payload hash is invalid.")
        with zipfile.ZipFile(io.BytesIO(cls.payload)) as archive:
            identity = json.loads(archive.read("version.json").decode("utf-8-sig"))
        cls.incoming_version = identity["version"]
        source_identity = json.loads((SOURCE_ROOT / "version.json").read_text(encoding="utf-8-sig"))
        minimum = max((1, 1, 0), update_manager._version(source_identity["version"]))
        if update_manager._version(cls.incoming_version) < minimum:
            raise unittest.SkipTest(
                f"Setup {cls.incoming_version} is stale; set PNG_MOCKUP_TEST_SETUP to the current 1.1+ stage "
                "with /update, strict version/channel validation and private runtime."
            )
        if identity.get("product_id") != "png-mockup" or identity.get("name") != "PNG Mockup":
            raise AssertionError("The selected setup is a different product.")
        build_root.mkdir(exist_ok=True)
        cls.work = (build_root / ("update-flow-fixture-" + uuid.uuid4().hex)).resolve()
        if build_root not in cls.work.parents:
            raise AssertionError("Fixture directory escaped the project build directory.")
        cls.work.mkdir()
        cls.uninstall_registry_key = "Software\\PNGMockupTests\\" + uuid.uuid4().hex + "\\png-mockup"
        cls.previous_registry_override = os.environ.get("PNG_MOCKUP_TEST_UNINSTALL_KEY")
        os.environ["PNG_MOCKUP_TEST_UNINSTALL_KEY"] = cls.uninstall_registry_key
        (cls.work / "FIXTURE-NOT-A-RELEASE.txt").write_text(
            "Simulated update endpoint and old version. No live GitHub download.\n"
            f"Test-only repository: {TEST_REPOSITORY}\n"
            "These repacked unsigned executables are test fixtures only; do not distribute them.\n",
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        for key in (cls.uninstall_registry_key, cls.uninstall_registry_key.rsplit("\\", 1)[0], "Software\\PNGMockupTests"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except (FileNotFoundError, OSError):
                pass
        if cls.previous_registry_override is None:
            os.environ.pop("PNG_MOCKUP_TEST_UNINSTALL_KEY", None)
        else:
            os.environ["PNG_MOCKUP_TEST_UNINSTALL_KEY"] = cls.previous_registry_override

    def make_fixture(self, version: str, name: str) -> tuple[Path, dict]:
        manifest = {"format": 1, "product_id": "png-mockup", "version": version, "files": []}
        payload = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.payload)) as source, zipfile.ZipFile(
            payload, "w", compression=zipfile.ZIP_DEFLATED,
        ) as target:
            for member in source.infolist():
                if member.filename == "setup_manifest.json":
                    continue
                content = source.read(member)
                if member.filename == "version.json":
                    content = encoded({"product_id": "png-mockup", "name": "PNG Mockup", "version": version})
                elif member.filename == "update-channel.json":
                    content = encoded({
                        "schema_version": 1, "product_id": "png-mockup",
                        "repository": TEST_REPOSITORY, "enabled": True,
                    })
                target.writestr(member.filename, content)
                manifest["files"].append({
                    "path": member.filename, "length": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
            target.writestr("setup_manifest.json", encoded(manifest))
        zipped = payload.getvalue()
        destination = self.work / name
        destination.write_bytes(self.stub + zipped + FOOTER.pack(
            len(zipped), hashlib.sha256(zipped).digest(), b"PNGMSP01",
        ))
        return destination, manifest

    def run_report(self, executable: Path, *args: str, timeout: int = 60, env: dict | None = None) -> dict:
        report = self.work / ("command-" + uuid.uuid4().hex + ".json")
        completed = subprocess.run(
            [str(executable), *args, "/report", str(report)],
            cwd=self.work, env=env, timeout=timeout,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertTrue(report.is_file(), "The actual executable did not produce a report.")
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        self.assertEqual(completed.returncode, 0, result)
        self.assertTrue(result["ok"], result)
        return result

    def test_backend_cached_update_waits_then_real_installer_preserves_data_and_relocates_runtime(self) -> None:
        old_setup, _ = self.make_fixture(OLD_VERSION, "TEST-ONLY-old-0.0.1-setup.exe")
        incoming_setup, manifest = self.make_fixture(self.incoming_version, "TEST-ONLY-incoming-setup.exe")
        target = self.work / "Installed app - giữ dữ liệu"
        installed = self.run_report(old_setup, "/extract", str(target))
        self.assertEqual(Path(installed["target"]), target)
        self.assertEqual(update_manager.read_current_version(target), OLD_VERSION)

        sentinels = {
            "config.json": b'{"test_user_setting":"preserve exact bytes","output_width_px":2333}\r\n',
            "PNG/design.png": b"test-only PNG input sentinel",
            "Mockup/template.1S.psd": b"test-only PSD sentinel",
            "Output/finished.png": b"test-only output sentinel",
            "logs/user-log.txt": b"test-only retained log",
            "logs/queue.json": b"[]\n",
        }
        for relative, content in sentinels.items():
            path = target / relative
            # Installed config is Hidden; do not remove its flags to write it.
            with path.open("r+b" if path.exists() else "wb") as stream:
                stream.write(content)
                stream.truncate()
        before_source = {name: sha256(target / name) for name in ("version.json", "src/gui.py", "PNG Mockup.exe")}
        version = self.incoming_version
        release = update_manager.ReleaseInfo(
            version, "v" + version, "TEST FIXTURE ONLY — simulated endpoint.",
            f"https://github.com/{TEST_REPOSITORY}/releases/tag/v{version}",
            f"https://github.com/{TEST_REPOSITORY}/releases/download/v{version}/PNG-Mockup-Setup.exe",
            incoming_setup.stat().st_size, sha256(incoming_setup), TEST_REPOSITORY,
        )
        incoming_bytes = incoming_setup.read_bytes()
        messages = []
        # Mock only the HTTP opening boundary. All streaming, hashes, ZIP identity,
        # cache publication and actual installer launch remain production code.
        with patch.object(update_manager, "_open_url", return_value=FixtureResponse(incoming_bytes)) as endpoint:
            cached = update_manager.download_release(release, target, log=messages.append)
        self.assertIsNotNone(cached, messages)
        self.assertEqual(sha256(cached), release.sha256)
        self.assertEqual(endpoint.call_count, 1)
        self.assertEqual(endpoint.call_args.args[0], release.download_url)
        self.assertEqual(update_manager.read_current_version(target), OLD_VERSION)

        ready = self.work / "private-process-ready.json"
        close_request = self.work / "close.request"
        exit_report = self.work / "private-process-exit.json"
        wait_script = self.work / "simulated_gui_lifetime.py"
        wait_script.write_text(
            "import json, pathlib, sys, time\n"
            "ready, close_request, result = map(pathlib.Path, sys.argv[1:])\n"
            "ready.write_text(json.dumps({'executable':sys.executable}), encoding='utf-8')\n"
            "deadline = time.monotonic() + 20\n"
            "while not close_request.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.05)\n"
            "result.write_text(json.dumps({'closed_by_request':close_request.exists()}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        private_python = target / "runtime" / "python" / "python.exe"
        child = subprocess.Popen(
            [str(private_python), "-I", "-B", "-X", "utf8", str(wait_script), str(ready), str(close_request), str(exit_report)],
            cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        updater = None
        try:
            deadline = time.monotonic() + 4
            while not ready.is_file() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.05)
            self.assertTrue(ready.is_file(), "The private-runtime lifetime fixture did not start.")
            self.assertEqual(Path(json.loads(ready.read_text())["executable"]).resolve(), private_python.resolve())
            started = time.monotonic()
            updater = update_manager.launch_updater(cached, target, wait_pid=child.pid, relaunch=False)
            observation_deadline = time.monotonic() + 3
            while time.monotonic() < observation_deadline:
                self.assertIsNone(child.poll(), "The installer must not terminate the old private process.")
                self.assertIsNone(updater.poll(), "The installer exited before the requested process closed.")
                self.assertEqual(update_manager.read_current_version(target), OLD_VERSION)
                self.assertEqual(before_source, {name: sha256(target / name) for name in before_source})
                time.sleep(0.2)
            close_request.write_text("normal close requested", encoding="utf-8")
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertTrue(json.loads(exit_report.read_text())["closed_by_request"])
            self.assertEqual(updater.wait(timeout=60), 0, "Actual /update failed; inspect cache/install-result.json.")
            update_seconds = time.monotonic() - started
        finally:
            close_request.write_text("normal close requested", encoding="utf-8")
            if child.poll() is None:
                child.wait(timeout=22)  # The fixture always exits itself; never force-kill it.
            if updater is not None and updater.poll() is None:
                updater.wait(timeout=60)

        report_path = cached.parent / "install-result.json"
        self.assertTrue(report_path.is_file())
        result = json.loads(report_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["product_id"], result["version"]), ("png-mockup", version))
        self.assertEqual(Path(result["target"]), target)
        self.assertFalse(result["relaunch_requested"])
        self.assertTrue(result["config_preserved"] and result["user_data_preserved"])
        self.assertEqual(update_manager.read_current_version(target), version)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.uninstall_registry_key) as key:
            self.assertEqual(winreg.QueryValueEx(key, "DisplayVersion")[0], version)
            self.assertEqual(Path(winreg.QueryValueEx(key, "InstallLocation")[0]), target)
        self.assertFalse((target / "PNG Mockup").exists(), "Update created a nested installation.")
        for row in manifest["files"]:
            if row["path"] != "config.json":
                self.assertEqual(sha256(target / row["path"]), row["sha256"], row["path"])
        for relative, content in sentinels.items():
            self.assertEqual((target / relative).read_bytes(), content, relative)
        self.assertFalse(list(target.glob(".png-mockup-install-*")))

        hidden = 0x2
        shown = {path.name for path in target.iterdir() if not path.stat().st_file_attributes & hidden}
        self.assertEqual(shown, {"PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "PNG", "Mockup", "Output"})
        self.assertFalse((target / "PNG" / "design.png").stat().st_file_attributes & hidden)
        self.assertTrue((target / "config.json").stat().st_file_attributes & hidden)
        env = dict(os.environ)
        env.update({
            "PYTHONHOME": r"X:\nonexistent-global-python",
            "PYTHONPATH": str(self.work / "untrusted-python"),
            "TCL_LIBRARY": r"X:\nonexistent-tcl", "TK_LIBRARY": r"X:\nonexistent-tk",
            "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
        })
        runtime_report = self.run_report(target / "PNG Mockup.exe", "/self-test", env=env)
        self.assertTrue(runtime_report["isolated"] and runtime_report["private_paths_only"])
        self.assertTrue(runtime_report["launcher_verified"] and runtime_report["session_lock_used"])
        self.assertEqual(Path(runtime_report["executable"]).resolve(), private_python.resolve())
        for origin in runtime_report["module_origins"].values():
            self.assertIn(private_python.parent.resolve(), Path(origin).resolve().parents)

        (self.work / "update-flow-result.json").write_bytes(encoded({
            "ok": True, "verification_kind": "simulated endpoint and old-version fixture; actual installer/backend",
            "live_github_download": False, "photoshop_or_gui_started": False,
            "base_setup": str(self.setup), "base_setup_sha256": hashlib.sha256(self.binary).hexdigest(),
            "old_version": OLD_VERSION, "incoming_version": version, "test_repository": TEST_REPOSITORY,
            "installed_root": str(target), "cached_setup": str(cached), "installer_report": str(report_path),
            "all_payload_hashes_verified": True, "user_sentinels_preserved": list(sentinels),
            "no_nested_folder": True, "visibility_verified": True, "private_runtime_verified": True,
            "update_elapsed_seconds": round(update_seconds, 3),
        }))
        print(f"Simulated-endpoint / actual-installer integration report: {self.work / 'update-flow-result.json'}")


if __name__ == "__main__":
    unittest.main()

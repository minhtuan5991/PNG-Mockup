"""Exercise the real unsigned installer without opening its interactive UI.

All extraction, corrupt-fixture and junction tests stay in a unique build
subdirectory. Fixtures are retained there for inspection; no existing app,
Desktop shortcut, user profile, registry, or global Python is modified.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import time
import unittest
import uuid
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FOOTER = struct.Struct("<q32s8s")


@unittest.skipUnless(os.name == "nt", "The distributable installer targets Windows.")
class InstallerPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.setup = Path(os.environ.get("PNG_MOCKUP_TEST_SETUP", str(ROOT / "PNG Mockup Setup.exe"))).resolve()
        if cls.setup != ROOT / "PNG Mockup Setup.exe" and (ROOT / "build").resolve() not in cls.setup.parents:
            raise AssertionError("Only installer artifacts built in this project may be tested.")
        if not cls.setup.is_file():
            raise unittest.SkipTest("Build PNG Mockup Setup.exe before running installer integration tests.")
        # Match normal installer directories. tempfile.mkdtemp applies a private
        # ACL on Windows, preventing the user's GUI session from inspecting it.
        cls.work = (ROOT / "build" / ("installer-tests-" + uuid.uuid4().hex)).resolve()
        if (ROOT / "build").resolve() not in cls.work.parents:
            raise AssertionError("Installer test directory escaped this project.")
        cls.work.mkdir()
        cls.binary = cls.setup.read_bytes()
        length, digest, marker = FOOTER.unpack(cls.binary[-FOOTER.size:])
        if marker != b"PNGMSP01" or not 0 < length < len(cls.binary) - FOOTER.size:
            raise AssertionError("Installer footer is invalid.")
        cls.zip_bytes = cls.binary[-FOOTER.size - length:-FOOTER.size]
        if hashlib.sha256(cls.zip_bytes).digest() != digest:
            raise AssertionError("Installer payload SHA-256 is invalid.")
        cls.stub = cls.binary[:-FOOTER.size - length]
        with zipfile.ZipFile(io.BytesIO(cls.zip_bytes)) as archive:
            cls.manifest = json.loads(archive.read("setup_manifest.json"))
            cls.identity = json.loads(archive.read("version.json"))
            cls.channel = json.loads(archive.read("update-channel.json"))
        cls.expected_identity = json.loads((ROOT / "App Source" / "version.json").read_text(encoding="utf-8-sig"))

    def command(self, executable: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
        report = self.work / ("report-" + uuid.uuid4().hex + ".json")
        if executable.name == "Uninstall.exe" and "/uninstall" in args and "/shortcut-directory" not in args:
            desktop = self.work / "desktop-fixture"
            desktop.mkdir(exist_ok=True)
            args += ("/shortcut-directory", str(desktop))
        completed = subprocess.run([str(executable), *args, "/report", str(report)], cwd=self.work,
                                   env=env, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertTrue(report.is_file(), "The real executable did not write its verification report.")
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        deadline = time.monotonic() + 45
        while result.get("state") == "pending" and time.monotonic() < deadline:
            time.sleep(0.1)
            try:
                result = json.loads(report.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                pass
        self.assertNotEqual(result.get("state"), "pending", "Uninstall worker did not complete.")
        return completed.returncode, result

    def install(self, name: str) -> Path:
        target = self.work / name
        code, report = self.command(self.setup, "/extract", str(target))
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertEqual(Path(report["target"]), target)
        self.assertTrue((target / "PNG Mockup.exe").is_file())
        self.assertFalse((target / "PNG Mockup" / "PNG Mockup.exe").exists(), "The installer appended an unrequested directory.")
        return target

    def powershell(self, script: str) -> str:
        executable = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        script = "$ErrorActionPreference = 'Stop'\n[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n" + script
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def make_shortcut(self, directory: Path, target: Path) -> Path:
        directory.mkdir(exist_ok=True)
        shortcut = directory / "PNG Mockup.lnk"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        self.powershell("\n".join((
            "$shell = New-Object -ComObject WScript.Shell",
            "$link = $shell.CreateShortcut(" + quote(shortcut) + ")",
            "$link.TargetPath = " + quote(target),
            "$link.Save()",
            "[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link)",
            "[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)",
        )))
        return shortcut

    def invoke_shortcut(self, target: Path, directory: Path, *, hold_existing: bool = False) -> dict:
        # Invoke the same public method from the actual compiled setup, without
        # opening its GUI or reinstalling 2,000 runtime files for each collision.
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        lines = [
            "$assembly = [Reflection.Assembly]::LoadFile(" + quote(self.setup) + ")",
            "$method = $assembly.GetType('InstallEngine', $true).GetMethod('CreateShortcut', [Reflection.BindingFlags]'Public, Static')",
            "$hold = $null",
        ]
        if hold_existing:
            lines.append("$hold = [IO.File]::Open(" + quote(directory / "PNG Mockup.lnk") + ", [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)")
        lines.extend((
            "try {",
            "  $value = $method.Invoke($null, [object[]]@(" + quote(target) + ", " + quote(directory) + "))",
            "  [pscustomobject]@{ok=$true;result=$value} | ConvertTo-Json -Compress",
            "} catch {",
            "  $errorMessage = if ($_.Exception.InnerException) { $_.Exception.InnerException.Message } else { $_.Exception.Message }",
            "  [pscustomobject]@{ok=$false;error=$errorMessage} | ConvertTo-Json -Compress",
            "} finally { if ($null -ne $hold) { $hold.Dispose() } }",
        ))
        return json.loads(self.powershell("\n".join(lines)))

    def fixture_exe(self, entries: list[tuple[str, bytes]], *, product_id: str = "png-mockup", bad_file_hash: bool = False) -> Path:
        manifest = {"format": 1, "product_id": product_id, "version": self.identity["version"], "files": []}
        if not any(name == "update-channel.json" for name, _ in entries):
            entries = entries + [("update-channel.json", json.dumps(self.channel).encode("utf-8"))]
        for name, data in entries:
            manifest["files"].append({"path": name, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        if bad_file_hash:
            manifest["files"][0]["sha256"] = "0" * 64
        data = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in entries:
                    archive.writestr(name, content)
                archive.writestr("setup_manifest.json", json.dumps(manifest).encode("utf-8"))
        payload = data.getvalue()
        executable = self.work / ("fixture-" + uuid.uuid4().hex + ".exe")
        executable.write_bytes(self.stub + payload + FOOTER.pack(len(payload), hashlib.sha256(payload).digest(), b"PNGMSP01"))
        return executable

    def test_actual_binary_verifies_identity_hashes_and_runtime_payload(self) -> None:
        code, report = self.command(self.setup, "/verify")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertEqual(report["product_id"], "png-mockup")
        self.assertEqual(report["version"], self.expected_identity["version"])
        self.assertTrue(report["offline_runtime_included"])
        self.assertEqual(report["files"], len(self.manifest["files"]) + 1)
        with zipfile.ZipFile(io.BytesIO(self.zip_bytes)) as archive:
            for row in self.manifest["files"]:
                content = archive.read(row["path"])
                self.assertEqual(len(content), row["length"], row["path"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), row["sha256"], row["path"])
                source_file = ROOT / ("payload-template" if row["path"] == "config.json" else "App Source") / row["path"]
                self.assertTrue(source_file.is_file(), "Payload file has no App Source counterpart: " + row["path"])
                self.assertEqual(hashlib.sha256(source_file.read_bytes()).hexdigest(), row["sha256"],
                                 "The setup is stale relative to App Source: " + row["path"])
            names = archive.namelist()
            self.assertFalse(any(name.split("/")[0] in {"PNG", "Mockup", "Output", "logs", "Theme", "tests"} for name in names))
            self.assertFalse(any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names))
            identity = json.loads(archive.read("version.json"))
            self.assertEqual(identity, self.expected_identity)
            config = json.loads(archive.read("config.json"))
            self.assertEqual(config, json.loads((ROOT / "payload-template" / "config.json").read_text(encoding="utf-8-sig")))
            channel = json.loads(archive.read("update-channel.json"))
            self.assertEqual(channel["product_id"], "png-mockup")
            self.assertEqual(channel["schema_version"], 1)
            self.assertTrue(channel["repository"] or not channel["enabled"])
        hash_file = self.setup.with_name(self.setup.name + ".sha256").read_text(encoding="ascii").split()[0]
        self.assertEqual(hash_file, hashlib.sha256(self.binary).hexdigest())

    def test_exact_unicode_install_location_and_isolated_runtime_relocation(self) -> None:
        target = self.install("Máy khác - kiểm tra PNG")
        for row in self.manifest["files"]:
            self.assertEqual(hashlib.sha256((target / row["path"]).read_bytes()).hexdigest(), row["sha256"], row["path"])
        for name in ("PNG", "Mockup", "Output", "logs"):
            self.assertTrue((target / name).is_dir())
        env = dict(os.environ)
        env.update({"PYTHONHOME": r"X:\missing-global-python", "PYTHONPATH": str(self.work / "untrusted-python"),
                    "TCL_LIBRARY": r"X:\missing-global-tcl", "TK_LIBRARY": r"X:\missing-global-tk",
                    "PATH": str(Path(os.environ["SystemRoot"]) / "System32")})
        code, report = self.command(target / "PNG Mockup.exe", "/self-test", env=env)
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"] and report["launcher_verified"] and report["isolated"] and report["private_paths_only"])
        runtime = (target / "runtime" / "python").resolve()
        self.assertEqual(Path(report["executable"]).resolve().parent, runtime)
        for origin in report["module_origins"].values():
            self.assertIn(runtime, Path(origin).resolve().parents)
        for path in report["search_paths"]:
            resolved = Path(path).resolve()
            self.assertTrue(resolved == runtime or runtime in resolved.parents or resolved == (target / "src").resolve())
        presets = subprocess.run([str(runtime / "python.exe"), "-I", "-B", "-X", "utf8", "-c",
                                  "import json,ui_themes; print(json.dumps({'styles':list(ui_themes.UI_THEMES),'presets':list(ui_themes.COLOR_PRESETS)}))"],
                                 cwd=self.work, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(presets.returncode, 0, presets.stderr)
        themes = json.loads(presets.stdout)
        self.assertEqual(themes["styles"], ["png_modern"])
        self.assertEqual(set(themes["presets"]), {"mint", "soft_purple", "light", "dark", "brown"})

    def test_reinstallation_preserves_config_user_folders_and_logs(self) -> None:
        target = self.install("Cài lại - giữ dữ liệu")
        sentinels = {"config.json": b'{"custom_config":"preserve these exact bytes"}\r\n'}
        for name in ("PNG", "Mockup", "Output", "logs"):
            sentinels[f"{name}/sentinel.bin"] = (name + " original user data\r\n").encode("utf-8")
        sentinels["logs/queue.json"] = b'{"queue":"keep"}'
        for relative, content in sentinels.items():
            path = target / relative
            with path.open("r+b" if path.exists() else "wb") as stream:
                stream.write(content)
                stream.truncate()
        (target / "src" / "gui.py").write_text("# simulated older program file\n", encoding="utf-8")
        shortcut = self.make_shortcut(self.work / "reinstall-shortcut-collision", self.setup)
        original_shortcut = shortcut.read_bytes()
        code, report = self.command(self.setup, "/extract", str(target), "/shortcut-directory", str(shortcut.parent))
        self.assertEqual(code, 0, report)
        self.assertTrue(report["config_preserved"] and report["user_data_preserved"])
        self.assertEqual(report["shortcut_result"], "preserved_or_not_created")
        self.assertIn("preserved", report["shortcut_warning"])
        self.assertEqual(shortcut.read_bytes(), original_shortcut)
        for relative, content in sentinels.items():
            self.assertEqual((target / relative).read_bytes(), content, relative)
        gui_row = next(row for row in self.manifest["files"] if row["path"] == "src/gui.py")
        self.assertEqual(hashlib.sha256((target / "src" / "gui.py").read_bytes()).hexdigest(), gui_row["sha256"])
        self.assertFalse(list(target.glob(".png-mockup-install-*")))

    def test_launcher_reports_missing_private_runtime_without_fallback(self) -> None:
        target = self.work / "missing-private-runtime"
        (target / "src").mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(self.zip_bytes)) as archive:
            (target / "PNG Mockup.exe").write_bytes(archive.read("PNG Mockup.exe"))
            (target / "version.json").write_bytes(archive.read("version.json"))
        (target / "src" / "gui.py").write_text("raise AssertionError('No global interpreter may run this')\n", encoding="utf-8")
        code, report = self.command(target / "PNG Mockup.exe", "/self-test")
        self.assertEqual(code, 2, report)
        self.assertFalse(report["ok"])
        self.assertIn("runtime", report["error"])

    def test_foreign_nonempty_and_source_targets_are_rejected_before_writing(self) -> None:
        foreign = self.work / "foreign-app"
        foreign.mkdir()
        (foreign / "version.json").write_text('{"product_id":"different-product"}', encoding="utf-8")
        unidentified = self.work / "unidentified-data"
        unidentified.mkdir()
        (unidentified / "sentinel.txt").write_text("untouched", encoding="utf-8")
        workspace = self.work / "source-project"
        source = workspace / "App Source"
        source.mkdir(parents=True)
        (workspace / "build_setup.ps1").write_text("# source marker", encoding="utf-8")
        (source / "version.json").write_text('{"product_id":"png-mockup"}', encoding="utf-8")
        for target in (foreign, unidentified, workspace, source, source / "nested"):
            with self.subTest(target=target.name):
                code, report = self.command(self.setup, "/extract", str(target))
                self.assertNotEqual(code, 0, report)
                self.assertFalse(report["ok"])
                self.assertFalse((target / "runtime").exists())
        self.assertEqual((unidentified / "sentinel.txt").read_text(encoding="utf-8"), "untouched")

    def test_unc_root_and_relative_targets_are_rejected(self) -> None:
        for target in ("relative", "C:\\", "//not-a-server.invalid/share/PNG Mockup", "\\\\not-a-server.invalid/share/PNG Mockup"):
            with self.subTest(target=target):
                code, report = self.command(self.setup, "/extract", target)
                self.assertNotEqual(code, 0, report)
                self.assertFalse(report["ok"])

    def test_junction_target_and_descendant_cannot_escape_install_root(self) -> None:
        outside = self.work / "junction-sentinel"
        outside.mkdir()
        (outside / "keep.txt").write_text("untouched", encoding="utf-8")
        target = self.work / "junction-app"
        target.mkdir()
        (target / "version.json").write_text(json.dumps(self.identity), encoding="utf-8")
        junction = target / "src"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        ps = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        script = "New-Item -ItemType Junction -Path " + quote(junction) + " -Target " + quote(outside) + " -ErrorAction Stop | Out-Null"
        made = subprocess.run([str(ps), "-NoProfile", "-Command", script], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(made.returncode, 0, made.stderr.decode("utf-8", errors="replace"))
        self.assertTrue(junction.is_junction())
        for selected in (junction, target):
            code, report = self.command(self.setup, "/extract", str(selected))
            self.assertNotEqual(code, 0, report)
            self.assertIn("junction", report["error"].lower())
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "untouched")
        self.assertEqual({path.name for path in outside.iterdir()}, {"keep.txt"})
        junction.rmdir()  # Remove only this junction, not the sentinel directory.

    def test_corrupt_payload_hash_is_rejected(self) -> None:
        bad = bytearray(self.binary)
        bad[-FOOTER.size - len(self.zip_bytes) + 10] ^= 1
        path = self.work / "corrupt-payload.exe"
        path.write_bytes(bad)
        code, report = self.command(path, "/verify")
        self.assertNotEqual(code, 0, report)
        self.assertIn("SHA-256 mismatch", report["error"])

    def test_forbidden_traversal_duplicate_and_user_data_archive_paths_are_rejected(self) -> None:
        paths = ("../escaped.txt", "/absolute.txt", "C:drive-relative.txt", "src/../escape.py", "src/trailing.",
                 "src/AUX.txt", "PNG/user-file.png", "logs/queue.json", "src/empty//file.py")
        for path in paths:
            with self.subTest(path=path):
                executable = self.fixture_exe([(path, b"fixture")])
                code, report = self.command(executable, "/verify")
                self.assertNotEqual(code, 0, report)
                self.assertFalse(report["ok"])
        duplicate = self.fixture_exe([("src/gui.py", b"one"), ("src\\gui.py", b"two")])
        code, report = self.command(duplicate, "/verify")
        self.assertNotEqual(code, 0, report)
        self.assertIn("Duplicate payload path", report["error"])

    def test_wrong_product_and_manifest_file_hash_are_rejected(self) -> None:
        candidates = (
            (self.fixture_exe([("src/gui.py", b"fixture")], product_id="foreign-product"), "different product"),
            (self.fixture_exe([("src/gui.py", b"fixture")], bad_file_hash=True), "SHA-256 mismatch"),
            (self.fixture_exe([("version.json", json.dumps({**self.identity, "product_id": "foreign-product"}).encode("utf-8"))]), "identity/version"),
            (self.fixture_exe([("version.json", json.dumps(self.identity).encode("utf-8"))]), "Required offline component"),
        )
        for executable, expected in candidates:
            with self.subTest(expected=expected):
                code, report = self.command(executable, "/verify")
                self.assertNotEqual(code, 0, report)
                self.assertIn(expected, report["error"])

    def test_parent_selection_creates_product_child_without_nested_reinstall(self) -> None:
        parent = self.work / "Selected parent folder"
        parent.mkdir()
        code, preview = self.command(self.setup, "/resolve", str(parent))
        self.assertEqual(code, 0, preview)
        target = parent / "PNG Mockup"
        self.assertEqual(Path(preview["target"]), target)
        self.assertFalse(target.exists(), "Preview must not create the target.")
        desktop = self.work / "parent-selection-shortcut"
        desktop.mkdir()
        code, installed = self.command(self.setup, "/install", str(parent), "/shortcut-directory", str(desktop))
        self.assertEqual(code, 0, installed)
        self.assertEqual(Path(installed["target"]), target)
        self.assertEqual(installed["shortcut_result"], "created_shortcut")
        code, existing = self.command(self.setup, "/resolve", str(target))
        self.assertEqual(code, 0, existing)
        self.assertEqual(Path(existing["target"]), target)
        code, reinstalled = self.command(self.setup, "/install", str(target), "/shortcut-directory", str(desktop))
        self.assertEqual(code, 0, reinstalled)
        self.assertEqual(reinstalled["shortcut_result"], "updated_matching_shortcut")
        self.assertFalse((target / "PNG Mockup").exists())

    def test_shortcut_creation_preserves_foreign_malformed_folder_and_locked_collisions(self) -> None:
        target = self.work / "Shortcut safety fixture"
        target.mkdir()
        with zipfile.ZipFile(io.BytesIO(self.zip_bytes)) as archive:
            for name in ("PNG Mockup.exe", "version.json"):
                (target / name).write_bytes(archive.read(name))
            (target / "tools").mkdir()
            helper = target / "tools" / "setup_shortcuts.ps1"
            helper.write_bytes(archive.read("tools/setup_shortcuts.ps1"))
        valid = self.work / "shortcut-valid"
        valid.mkdir()
        self.assertEqual(self.invoke_shortcut(target, valid), {"ok": True, "result": "created_shortcut"})
        self.assertEqual(self.invoke_shortcut(target, valid), {"ok": True, "result": "updated_matching_shortcut"})
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        helper_output = self.powershell("& " + quote(helper) + " -DesktopPath " + quote(valid))
        self.assertIn("updated_matching_shortcut", helper_output)
        before = (valid / "PNG Mockup.lnk").read_bytes()
        locked = self.invoke_shortcut(target, valid, hold_existing=True)
        self.assertFalse(locked["ok"], locked)
        self.assertEqual((valid / "PNG Mockup.lnk").read_bytes(), before)

        foreign = self.make_shortcut(self.work / "shortcut-foreign", self.setup)
        original_foreign = foreign.read_bytes()
        helper_rejected = self.powershell("try { & " + quote(helper) + " -DesktopPath " + quote(foreign.parent) +
                                          "\nthrow 'Unexpected helper success.' } catch { $_.Exception.Message }")
        self.assertIn("preserved", helper_rejected)
        self.assertEqual(foreign.read_bytes(), original_foreign)
        malformed = self.work / "shortcut-malformed" / "PNG Mockup.lnk"
        malformed.parent.mkdir()
        malformed.write_bytes(b"User notes with a .lnk extension; do not replace.")
        for shortcut in (foreign, malformed):
            with self.subTest(path=shortcut.parent.name):
                before = shortcut.read_bytes()
                result = self.invoke_shortcut(target, shortcut.parent)
                self.assertFalse(result["ok"], result)
                self.assertEqual(shortcut.read_bytes(), before)
                self.assertFalse(list(shortcut.parent.glob(".png-mockup-shortcut-*.lnk")))

        folder = self.work / "shortcut-folder" / "PNG Mockup.lnk"
        folder.mkdir(parents=True)
        (folder / "user-note.txt").write_text("keep this folder", encoding="utf-8")
        result = self.invoke_shortcut(target, folder.parent)
        self.assertFalse(result["ok"], result)
        self.assertIn("folder", result["error"])
        self.assertEqual((folder / "user-note.txt").read_text(encoding="utf-8"), "keep this folder")

    def test_technical_visibility_only_changes_owned_top_level_hidden_bits(self) -> None:
        import ctypes
        target = self.install("Visibility attributes")
        hidden = 0x2
        readonly = 0x1
        system = 0x4
        visible = {"PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "PNG", "Mockup", "Output"}
        shown = {path.name for path in target.iterdir() if not path.stat().st_file_attributes & hidden}
        self.assertEqual(shown, visible)
        user_file = target / "my-notes.txt"
        user_file.write_text("keep my visibility", encoding="utf-8")
        config = target / "config.json"
        attrs_before = {path.name: path.stat().st_file_attributes for path in target.iterdir()}
        set_attributes = ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW
        set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        set_attributes.restype = ctypes.c_int
        self.assertTrue(set_attributes(str(config), attrs_before["config.json"] | readonly | system))
        try:
            nested = target / "src" / "gui.py"
            nested_before = nested.stat().st_file_attributes
            original_config = config.read_bytes()
            code, report = self.command(target / "Show technical files.exe", "/show")
            self.assertEqual(code, 0, report)
            self.assertTrue(report["ok"] and report["visible"])
            self.assertEqual(config.stat().st_file_attributes & (hidden | readonly | system), readonly | system)
            self.assertEqual(nested.stat().st_file_attributes, nested_before)
            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual(user_file.stat().st_file_attributes, attrs_before["my-notes.txt"])
            code, report = self.command(target / "Show technical files.exe", "/toggle")
            self.assertEqual(code, 0, report)
            self.assertFalse(report["visible"])
            self.assertEqual(config.stat().st_file_attributes & (hidden | readonly | system), hidden | readonly | system)
            for name in visible:
                self.assertFalse((target / name).stat().st_file_attributes & hidden)
            self.assertFalse(user_file.stat().st_file_attributes & hidden)
            code, report = self.command(target / "Show technical files.exe", "/status")
            self.assertEqual(code, 0, report)
            self.assertFalse(report["visible"])
            self.assertEqual(report["changed"], [])
        finally:
            self.assertTrue(set_attributes(str(config), attrs_before["config.json"]))

    def test_visibility_and_uninstall_helpers_refuse_app_source(self) -> None:
        source = ROOT / "App Source"
        before = {path.name: path.stat().st_file_attributes for path in source.iterdir()}
        for helper, args in (("Show technical files.exe", ("/hide",)), ("Uninstall.exe", ("/uninstall", "/yes"))):
            code, report = self.command(source / helper, *args)
            self.assertNotEqual(code, 0, report)
            self.assertIn("source", report["error"].lower())
        self.assertEqual({path.name: path.stat().st_file_attributes for path in source.iterdir()}, before)

    def test_uninstall_requires_confirmation_then_preserves_data_unknown_and_modified_files(self) -> None:
        target = self.install("Uninstall keep user data")
        before = (target / "config.json").read_bytes()
        for name in ("PNG", "Mockup", "Output", "logs"):
            (target / name / "user-file.bin").write_bytes(("owned by user " + name).encode("utf-8"))
        unknown = target / "src" / "my-custom-file.py"
        unknown.write_text("# user extension\n", encoding="utf-8")
        modified = target / "src" / "gui.py"
        modified.write_text("# locally edited program file: keep it\n", encoding="utf-8")
        desktop = self.work / "desktop-fixture"
        desktop.mkdir(exist_ok=True)
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        shortcut = subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(target / "tools" / "setup_shortcuts.ps1"), "-DesktopPath", str(desktop)],
                                  capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(shortcut.returncode, 0, shortcut.stderr)
        self.assertTrue((desktop / "PNG Mockup.lnk").is_file())
        code, preview = self.command(target / "Uninstall.exe", "/preview")
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview["state"], "preview")
        self.assertNotIn("config.json", preview["candidate_files"])
        code, rejected = self.command(target / "Uninstall.exe", "/uninstall")
        self.assertNotEqual(code, 0, rejected)
        self.assertIn("confirmation", rejected["error"])
        self.assertTrue((target / "PNG Mockup.exe").is_file())
        code, removed = self.command(target / "Uninstall.exe", "/uninstall", "/yes")
        self.assertEqual(code, 0, removed)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(removed["state"], "uninstalled")
        self.assertTrue(removed["user_data_preserved"])
        self.assertEqual(removed["shortcut_result"], "removed_matching_shortcut")
        self.assertFalse((desktop / "PNG Mockup.lnk").exists())
        for name in ("PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "setup_manifest.json", "version.json"):
            self.assertFalse((target / name).exists(), name)
        self.assertEqual((target / "config.json").read_bytes(), before)
        self.assertTrue(unknown.is_file())
        self.assertEqual(modified.read_text(encoding="utf-8"), "# locally edited program file: keep it\n")
        self.assertIn("src/gui.py", removed["modified_files_preserved"])
        for name in ("PNG", "Mockup", "Output", "logs"):
            self.assertEqual((target / name / "user-file.bin").read_bytes(), ("owned by user " + name).encode("utf-8"))
        receipt = json.loads((target / "logs" / ".installation.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((receipt["product_id"], receipt["state"]), ("png-mockup", "uninstalled"))
        code, restored = self.command(self.setup, "/install", str(target))
        self.assertEqual(code, 0, restored)
        self.assertEqual(Path(restored["target"]), target)
        self.assertTrue(restored["config_preserved"])
        self.assertTrue(unknown.is_file())
        self.assertEqual((target / "config.json").read_bytes(), before)

    def test_installer_and_uninstaller_refuse_a_held_session_lock(self) -> None:
        target = self.install("Session lock guards")
        files_before = {name: hashlib.sha256((target / name).read_bytes()).hexdigest() for name in ("PNG Mockup.exe", "src/gui.py", "config.json")}
        # Python's open handle is enough to conflict a new FileShare.None open.
        with (target / "logs" / ".app-session.lock").open("r+b"):
            for executable, args in ((self.setup, ("/extract", str(target))), (target / "Uninstall.exe", ("/uninstall", "/yes")),
                                     (target / "PNG Mockup.exe", ("/self-test",))):
                code, result = self.command(executable, *args)
                self.assertNotEqual(code, 0, result)
                self.assertIn("running", result["error"].lower())
        self.assertEqual(files_before, {name: hashlib.sha256((target / name).read_bytes()).hexdigest() for name in files_before})

    def test_launcher_holds_lock_until_its_diagnostic_python_child_exits(self) -> None:
        target = self.install("Launcher lifetime")
        report = self.work / "launcher-lifetime.json"
        # Some process hosts supply both casings in the native environment.
        # The launcher must avoid .NET's duplicate-key environment dictionary.
        env = dict(os.environ)
        env["Path"] = env.get("PATH", str(Path(os.environ["SystemRoot"]) / "System32"))
        env["PATH"] = env["Path"]
        child = subprocess.Popen([str(target / "PNG Mockup.exe"), "/self-test", "/hold-ms", "3500", "/report", str(report)],
                                 cwd=self.work, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 10
        locked = False
        while time.monotonic() < deadline and child.poll() is None:
            try:
                with (target / "logs" / ".app-session.lock").open("r+b"):
                    pass
            except PermissionError:
                locked = True
                break
            time.sleep(0.05)
        failure_detail = report.read_text(encoding="utf-8-sig") if report.exists() else "No runtime report was written."
        self.assertTrue(locked, "Launcher never acquired its exclusive session lock. " + failure_detail)
        self.assertIsNone(child.poll(), "Launcher exited before the diagnostic child.")
        self.assertEqual(child.wait(timeout=15), 0)
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        self.assertTrue(result["session_lock_used"])
        with (target / "logs" / ".app-session.lock").open("r+b"):
            pass

    def test_update_rejects_equal_downgrade_and_disabled_or_missing_channels(self) -> None:
        target = self.install("Update exact folder")
        before = (target / "config.json").read_bytes()
        for version in (self.identity["version"], "999.0.0"):
            with (target / "version.json").open("r+b") as stream:
                stream.write(json.dumps({**self.identity, "version": version}).encode("utf-8"))
                stream.truncate()
            code, rejected = self.command(self.setup, "/update", str(target), "/wait-pid", str(os.getpid()))
            self.assertNotEqual(code, 0, rejected)
            self.assertIn("strictly newer", rejected["error"])
        with (target / "version.json").open("r+b") as stream:
            stream.write(json.dumps({**self.identity, "version": "0.0.1"}).encode("utf-8"))
            stream.truncate()
        with (target / "update-channel.json").open("r+b") as stream:
            stream.write(json.dumps({**self.channel, "enabled": False}).encode("utf-8"))
            stream.truncate()
        code, rejected = self.command(self.setup, "/update", str(target), "/wait-pid", str(os.getpid()))
        self.assertNotEqual(code, 0, rejected)
        self.assertIn("enabled", rejected["error"])
        (target / "update-channel.json").unlink()
        code, rejected = self.command(self.setup, "/update", str(target), "/wait-pid", str(os.getpid()))
        self.assertNotEqual(code, 0, rejected)
        self.assertIn("update-channel", rejected["error"])
        self.assertEqual((target / "config.json").read_bytes(), before)
        self.assertFalse((target / "PNG Mockup").exists())


if __name__ == "__main__":
    unittest.main()

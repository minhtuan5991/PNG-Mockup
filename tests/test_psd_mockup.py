from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

import psd_mockup  # noqa: E402
import diagnose  # noqa: E402
from config import app_temp_root  # noqa: E402


class _FakeRegistryKey:
    def __init__(self, view: int, path: str) -> None:
        self.view = view
        self.path = path

    def __enter__(self) -> _FakeRegistryKey:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeWinreg:
    HKEY_CLASSES_ROOT = object()
    KEY_READ = 0x0001
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200

    def __init__(self) -> None:
        self.names = {
            self.KEY_WOW64_64KEY: [
                "Unrelated.Application",
                "Photoshop.Application",
                "Photoshop.Application.160",
                "Photoshop.Application.170.1",
                "Photoshop.Application.170",
            ],
            self.KEY_WOW64_32KEY: [
                "Photoshop.Application.180",
                "Photoshop.Application.170",
            ],
            0: [],
        }
        self.curver = {self.KEY_WOW64_64KEY: "Photoshop.Application.170"}

    def OpenKey(
        self,
        root: object,
        path: str,
        _reserved: int = 0,
        access: int = 0,
    ) -> _FakeRegistryKey:
        if root is not self.HKEY_CLASSES_ROOT:
            raise FileNotFoundError(path)
        view = access & (self.KEY_WOW64_64KEY | self.KEY_WOW64_32KEY)
        if path == "":
            return _FakeRegistryKey(view, path)
        if path == rf"{psd_mockup.PHOTOSHOP_GENERIC_PROGID}\CurVer" and view in self.curver:
            return _FakeRegistryKey(view, path)
        raise FileNotFoundError(path)

    def QueryValueEx(self, key: _FakeRegistryKey, _name: str) -> tuple[str, int]:
        return self.curver[key.view], 1

    def EnumKey(self, key: _FakeRegistryKey, index: int) -> str:
        try:
            return self.names[key.view][index]
        except IndexError as exc:
            raise OSError("end") from exc


class PhotoshopProgIdTests(unittest.TestCase):
    def tearDown(self) -> None:
        psd_mockup.find_photoshop_progids.cache_clear()

    def test_discovers_curver_latest_versions_and_deduplicates_registry_views(self) -> None:
        progids = psd_mockup._discover_photoshop_progids(_FakeWinreg())

        self.assertEqual(
            progids,
            (
                "Photoshop.Application.170",
                "Photoshop.Application.180",
                "Photoshop.Application.170.1",
                "Photoshop.Application.160",
                "Photoshop.Application",
            ),
        )
        self.assertEqual(progids.count("Photoshop.Application.170"), 1)

    def test_registry_discovery_is_cached(self) -> None:
        fake_winreg = _FakeWinreg()
        psd_mockup.find_photoshop_progids.cache_clear()
        with patch.dict(sys.modules, {"winreg": fake_winreg}):
            first = psd_mockup.find_photoshop_progids()
            fake_winreg.names[fake_winreg.KEY_WOW64_64KEY].append(
                "Photoshop.Application.999"
            )
            second = psd_mockup.find_photoshop_progids()

        self.assertIs(first, second)
        self.assertNotIn("Photoshop.Application.999", second)
        self.assertEqual(psd_mockup.find_photoshop_progids.cache_info().hits, 1)

    def test_generic_progid_is_retained_when_registry_has_no_photoshop_classes(self) -> None:
        with patch.object(psd_mockup, "find_photoshop_progids", return_value=()):
            self.assertEqual(
                psd_mockup._photoshop_progid_candidates(),
                ("Photoshop.Application",),
            )

    def test_vbs_attaches_then_creates_each_registered_progid(self) -> None:
        script = psd_mockup._photoshop_vbs(
            Path(r"C:\Temp\run_photoshop.jsx"),
            ("Photoshop.Application.170", "Photoshop.Application"),
        )

        self.assertIn(
            'progIds = Array("Photoshop.Application.170", "Photoshop.Application")',
            script,
        )
        self.assertIn("Set app = GetObject(, progId)", script)
        self.assertIn("Set app = CreateObject(progId)", script)
        self.assertLess(script.index("GetObject"), script.index("CreateObject"))
        self.assertIn("WScript.Quit 2", script)
        self.assertIn("WScript.Quit 3", script)
        self.assertNotIn("Photoshop.exe", script)

    def test_jsx_restores_photoshop_dialog_mode_after_each_job(self) -> None:
        script = psd_mockup.PHOTOSHOP_JSX.read_text(encoding="utf-8")

        self.assertIn("var previousDisplayDialogs = app.displayDialogs;", script)
        self.assertIn("app.displayDialogs = DialogModes.NO;", script)
        self.assertIn("app.displayDialogs = previousDisplayDialogs;", script)


class PhotoshopRunFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project_root = self.root / "app"
        self.output_dir = self.project_root / "Output"
        self.logs_dir = self.project_root / "logs"
        self.output_dir.mkdir(parents=True)
        self.logs_dir.mkdir()
        self.mockup_path = self.project_root / "shirt.1S.psd"
        self.design_path = self.project_root / "design.png"
        self.mockup_path.write_bytes(b"psd")
        self.design_path.write_bytes(b"png")
        self.config = SimpleNamespace(
            project_root=self.project_root,
            output_dir=self.output_dir,
            logs_dir=self.logs_dir,
            generation_timeout_sec=10,
            output_watermark_enabled=False,
            remove_output_metadata=False,
        )
        self.job = {
            "id": "000001",
            "output_name": "design__shirt__PSD_JOB.png",
            "psd_output_prefix": "design__shirt",
            "mockup_path": str(self.mockup_path),
            "psd_replacements": [
                {"path": str(self.design_path), "layer": "Design Dark"}
            ],
        }
        with patch.object(psd_mockup.tempfile, "gettempdir", return_value=str(self.root)):
            self.work_dir = app_temp_root(self.project_root) / "psd_jobs" / "design__shirt__PSD_JOB"

    def _run_with_cscript_result(self, returncode: int, stdout: str) -> None:
        with (
            patch.object(psd_mockup.tempfile, "gettempdir", return_value=str(self.root)),
            patch.object(
                psd_mockup,
                "find_photoshop_exe",
                return_value=Path(r"C:\Program Files\Adobe\Adobe Photoshop 2023\Photoshop.exe"),
            ),
            patch.object(
                psd_mockup,
                "find_photoshop_progids",
                return_value=("Photoshop.Application.170",),
            ),
            patch.object(
                psd_mockup.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=returncode, stdout=stdout),
            ),
            patch.object(psd_mockup.subprocess, "Popen") as popen,
        ):
            try:
                psd_mockup.run_psd_job(self.config, self.job, MagicMock())
            finally:
                popen.assert_not_called()

    def test_exit_code_2_removes_stale_result_and_gives_repair_guidance(self) -> None:
        self.work_dir.mkdir(parents=True)
        stale_result = self.work_dir / "result.json"
        stale_result.write_text('{"ok": true}', encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "Creative Cloud") as raised:
            self._run_with_cscript_result(
                2,
                "Cannot start Adobe Photoshop COM automation: ActiveX component can't create object",
            )

        self.assertFalse(stale_result.exists())
        message = str(raised.exception)
        self.assertIn("Photoshop.Application.170", message)
        self.assertIn("tài khoản Windows", message)
        self.assertIn("giấy phép", message)
        self.assertIn("Không cần tài khoản GitHub", message)
        vbs = (self.work_dir / "run_photoshop.vbs").read_text(encoding="utf-8")
        self.assertIn("Photoshop.Application.170", vbs)
        self.assertIn("Photoshop.Application", vbs)

    def test_exit_code_3_is_reported_as_jsx_failure_without_exe_fallback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "JSX script failed") as raised:
            self._run_with_cscript_result(3, "Photoshop script failed: bad smart object")

        self.assertIn("bad smart object", str(raised.exception))


class PhotoshopDiagnosticTests(unittest.TestCase):
    def test_preflight_reports_exe_without_com_as_not_ready_without_launching(self) -> None:
        photoshop_exe = Path(r"C:\Program Files\Adobe\Adobe Photoshop 2023\Photoshop.exe")
        with (
            patch.object(diagnose, "find_photoshop_exe", return_value=photoshop_exe),
            patch.object(diagnose, "find_photoshop_progids", return_value=()),
            patch.object(psd_mockup.subprocess, "Popen") as popen,
            patch.object(psd_mockup.subprocess, "run") as run,
        ):
            result = diagnose._photoshop_check(required=True)

        self.assertFalse(result.ok)
        self.assertTrue(result.required)
        self.assertIn("COM automation is not registered", result.message)
        self.assertIn("Creative Cloud", result.message)
        popen.assert_not_called()
        run.assert_not_called()

    def test_preflight_lists_photoshop_2023_progid_when_ready(self) -> None:
        photoshop_exe = Path(r"C:\Program Files\Adobe\Adobe Photoshop 2023\Photoshop.exe")
        with (
            patch.object(diagnose, "find_photoshop_exe", return_value=photoshop_exe),
            patch.object(
                diagnose,
                "find_photoshop_progids",
                return_value=("Photoshop.Application.170",),
            ),
        ):
            result = diagnose._photoshop_check(required=True)

        self.assertTrue(result.ok)
        self.assertIn("Photoshop.Application.170", result.message)


if __name__ == "__main__":
    unittest.main()

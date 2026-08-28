from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "App Source" / "src"))

from gui import PngMockupGui  # noqa: E402


class MainThreadValue:
    def __init__(self, value: object = "") -> None:
        self.owner = threading.get_ident()
        self.value = value

    def set(self, value: object) -> None:
        assert threading.get_ident() == self.owner, "Tk variable changed outside main thread"
        self.value = value

    def get(self) -> object:
        return self.value


class MainThreadButton:
    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.options = {"state": "normal"}

    def configure(self, **kwargs: object) -> None:
        assert threading.get_ident() == self.owner, "Tk widget changed outside main thread"
        self.options.update(kwargs)


class MainThreadRoot:
    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.callbacks: dict[str, tuple[object, tuple[object, ...]]] = {}
        self.counter = 0
        self.destroyed = False

    def after(self, delay: int, callback: object, *args: object) -> str:
        assert threading.get_ident() == self.owner, "Tk.after called outside main thread"
        self.counter += 1
        key = f"after-{self.counter}"
        self.callbacks[key] = (callback, args)
        return key

    def after_cancel(self, key: str) -> None:
        assert threading.get_ident() == self.owner
        self.callbacks.pop(key, None)

    def pump(self) -> None:
        callbacks = list(self.callbacks.items())
        for key, (callback, args) in callbacks:
            if key in self.callbacks:
                del self.callbacks[key]
                callback(*args)

    def update_idletasks(self) -> None:
        assert threading.get_ident() == self.owner

    def destroy(self) -> None:
        assert threading.get_ident() == self.owner
        self.destroyed = True


def make_gui() -> PngMockupGui:
    app = PngMockupGui.__new__(PngMockupGui)
    app.root = MainThreadRoot()
    app._initialize_update_state()
    app.current_process = None
    app.captured_task_running = False
    app._close_after_captured_run = False
    app._force_stop_requested = False
    app.maintenance_running = False
    app.status_var = MainThreadValue("Sẵn sàng")
    app.update_status_var = MainThreadValue()
    app.update_detail_var = MainThreadValue()
    app.update_progress_var = MainThreadValue(0)
    app.update_indicator_var = MainThreadValue()
    app.buttons = {key: MainThreadButton() for key in ("update", "install_update", "run", "stop", "force_stop")}
    app.locked_buttons = {"run"}
    app.output_watermark_option_widgets = []
    app.settings_controls = []
    app.update_output_watermark_controls = MagicMock()
    app._show_update_notice = MagicMock()
    app.write_output = MagicMock()
    return app


ENABLED = SimpleNamespace(enabled=True, repository="private/png-mockup", reason="")
DISABLED = SimpleNamespace(enabled=False, repository="", reason="Chưa cấu hình GitHub riêng.")
RELEASE = SimpleNamespace(version="1.2.0")
CACHE = Path("private-cache") / "PNG Mockup Setup.exe"


class GuiUpdateTests(unittest.TestCase):
    def settle(self, app: PngMockupGui) -> None:
        if app._update_thread is not None:
            app._update_thread.join(timeout=2)
            self.assertFalse(app._update_thread.is_alive(), "mock updater worker did not exit")
        for _ in range(5):
            app.root.pump()
            if not app.root.callbacks:
                break

    def test_every_startup_checks_once_without_loading_legacy_flags(self) -> None:
        app = make_gui()
        with (
            patch("gui.update_manager.read_channel", return_value=ENABLED, create=True),
            patch("gui.update_manager.check_latest_release", return_value=None) as check,
            patch("gui.load_config", side_effect=AssertionError("legacy config must not schedule updates")),
            patch("gui.messagebox.showinfo") as info,
            patch("gui.messagebox.showerror") as error,
        ):
            app._maybe_auto_check_updates()
            app._maybe_auto_check_updates()
            self.settle(app)
        check.assert_called_once()
        app._show_update_notice.assert_not_called()
        info.assert_not_called()
        error.assert_not_called()

    def test_disabled_channel_is_explicit_and_never_uses_network(self) -> None:
        app = make_gui()
        with (
            patch("gui.update_manager.read_channel", return_value=DISABLED, create=True),
            patch("gui.update_manager.check_latest_release") as check,
            patch("gui.messagebox.showinfo") as info,
        ):
            app.check_for_updates()
            self.settle(app)
        check.assert_not_called()
        info.assert_not_called()
        app._show_update_notice.assert_not_called()
        self.assertEqual(app.update_status_var.get(), DISABLED.reason)
        self.assertFalse(app.is_running())

    def test_offline_error_is_log_only_and_recovers_update_button(self) -> None:
        app = make_gui()
        with (
            patch("gui.update_manager.read_channel", return_value=ENABLED, create=True),
            patch("gui.update_manager.check_latest_release", side_effect=OSError("network unavailable")),
            patch("gui.messagebox.showerror") as error,
        ):
            app.check_for_updates(silent=True)
            self.settle(app)
        error.assert_not_called()
        app._show_update_notice.assert_not_called()
        self.assertFalse(app.update_check_running)
        self.assertEqual(app.buttons["update"].options["state"], "normal")
        self.assertIn("network unavailable", str(app.write_output.call_args_list))

    def test_download_runs_once_without_interfering_with_png_job_or_tk(self) -> None:
        app = make_gui()
        process = MagicMock()
        process.poll.return_value = None
        app.current_process = process
        app.captured_task_running = True
        entered = threading.Event()
        release_download = threading.Event()
        worker_ids = []

        def download(info, root, *, progress, cancel_event, log):
            worker_ids.append(threading.get_ident())
            progress(25, 100)
            log("received partial bytes")
            entered.set()
            release_download.wait(timeout=2)
            progress(100, 100)
            return CACHE

        with (
            patch("gui.update_manager.read_channel", return_value=ENABLED, create=True),
            patch("gui.update_manager.check_latest_release", return_value=RELEASE) as check,
            patch("gui.update_manager.download_release", side_effect=download) as fetch,
        ):
            try:
                app.check_for_updates()
                self.assertTrue(entered.wait(timeout=2))
                app.check_for_updates()
                app.root.pump()
                self.assertTrue(app.update_check_running)
                self.assertEqual(app.update_progress_var.get(), 25.0)
                self.assertIs(app.current_process, process)
                self.assertTrue(app.captured_task_running)
                self.assertEqual(app.status_var.get(), "Sẵn sàng")
                self.assertNotEqual(worker_ids[0], threading.get_ident())
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
            finally:
                release_download.set()
                self.settle(app)
        check.assert_called_once()
        fetch.assert_called_once()
        app._show_update_notice.assert_called_once_with("1.2.0")
        self.assertEqual(app._downloaded_update, CACHE)
        self.assertEqual(app.buttons["install_update"].options["state"], "disabled")
        app.captured_task_running = False
        app.current_process = None
        app.update_buttons()
        self.assertEqual(app.buttons["run"].options["state"], "normal")
        self.assertEqual(app.buttons["install_update"].options["state"], "normal")

    def test_background_check_never_locks_run_button(self) -> None:
        app = make_gui()
        app.update_check_running = True
        app.update_buttons()
        self.assertFalse(app.is_running())
        self.assertEqual(app.buttons["run"].options["state"], "normal")
        self.assertEqual(app.buttons["update"].options["state"], "disabled")

    def test_rechecking_same_version_does_not_show_a_second_notice(self) -> None:
        app = make_gui()
        with (
            patch("gui.update_manager.read_channel", return_value=ENABLED, create=True),
            patch("gui.update_manager.check_latest_release", return_value=RELEASE),
            patch("gui.update_manager.download_release", return_value=CACHE),
        ):
            for _ in range(2):
                app.check_for_updates()
                self.settle(app)
        app._show_update_notice.assert_called_once_with("1.2.0")

    def test_download_error_never_offers_install_or_shows_error_modal(self) -> None:
        app = make_gui()
        with (
            patch("gui.update_manager.read_channel", return_value=ENABLED, create=True),
            patch("gui.update_manager.check_latest_release", return_value=RELEASE),
            patch("gui.update_manager.download_release", return_value=None),
            patch("gui.messagebox.showerror") as error,
        ):
            app.check_for_updates()
            self.settle(app)
        error.assert_not_called()
        self.assertIsNone(app._downloaded_update)
        self.assertEqual(app.buttons["install_update"].options["state"], "disabled")

    def test_installer_is_not_launched_while_any_work_is_running(self) -> None:
        for state in ("captured_task_running", "maintenance_running", "update_check_running"):
            with self.subTest(state=state):
                app = make_gui()
                app._downloaded_update = CACHE
                setattr(app, state, True)
                with patch("gui.update_manager.launch_updater") as launch:
                    app.install_downloaded_update()
                launch.assert_not_called()
                self.assertFalse(app.root.destroyed)
                self.assertIsNone(app._install_after_close)

    def test_job_started_after_install_click_never_enters_force_stop(self) -> None:
        app = make_gui()
        process = MagicMock()
        process.poll.return_value = None
        app.current_process = process
        app.captured_task_running = True
        app._install_after_close = CACHE
        with (
            patch("gui.update_manager.launch_updater") as launch,
            patch("gui.messagebox.askyesno") as confirm,
        ):
            app.on_close()
        launch.assert_not_called()
        confirm.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertFalse(app.root.destroyed)
        self.assertIsNone(app._install_after_close)

    def test_outdated_worker_events_cannot_replace_ready_update(self) -> None:
        app = make_gui()
        app._update_run_counter = 2
        app._available_update = RELEASE
        app._downloaded_update = CACHE
        app._update_events.put((1, "available", SimpleNamespace(version="1.1.5")))
        app._update_events.put((1, "ready", (SimpleNamespace(version="1.1.5"), Path("stale.exe"))))
        app._poll_update_events()
        self.assertIs(app._available_update, RELEASE)
        self.assertEqual(app._downloaded_update, CACHE)
        app._show_update_notice.assert_not_called()

    def test_install_uses_actual_pid_only_after_cleanup_and_close_approval(self) -> None:
        app = make_gui()
        app._downloaded_update = CACHE
        operations = []
        with (
            patch("gui.load_config", return_value=SimpleNamespace(cleanup_on_exit=True)),
            patch("gui.cleanup_app_files", side_effect=lambda *a, **k: operations.append("cleanup")),
            patch("gui.update_manager.launch_updater", side_effect=lambda *a, **k: operations.append("install")) as launch,
        ):
            app.install_downloaded_update()
        self.assertEqual(operations, ["cleanup", "install"])
        self.assertEqual(launch.call_args.args[0], CACHE)
        self.assertEqual(launch.call_args.kwargs, {"wait_pid": os.getpid(), "relaunch": True})
        self.assertTrue(app.root.destroyed)
        self.assertTrue(app._update_cancel.is_set())

    def test_cancelled_close_never_launches_or_defers_install(self) -> None:
        app = make_gui()
        app._downloaded_update = CACHE
        with (
            patch("gui.load_config", return_value=SimpleNamespace(cleanup_on_exit=True)),
            patch("gui.cleanup_app_files", side_effect=OSError("cleanup blocked")),
            patch("gui.messagebox.askyesno", return_value=False),
            patch("gui.update_manager.launch_updater") as launch,
        ):
            app.install_downloaded_update()
        launch.assert_not_called()
        self.assertFalse(app.root.destroyed)
        self.assertFalse(app._update_cancel.is_set())
        self.assertIsNone(app._install_after_close)

    def test_installer_launch_failure_keeps_application_open(self) -> None:
        app = make_gui()
        app._downloaded_update = CACHE
        with (
            patch("gui.load_config", return_value=SimpleNamespace(cleanup_on_exit=False)),
            patch("gui.update_manager.launch_updater", side_effect=OSError("cannot execute")),
        ):
            app.install_downloaded_update()
        self.assertFalse(app.root.destroyed)
        self.assertFalse(app._closing)
        self.assertIsNone(app._install_after_close)
        self.assertIn("cannot execute", str(app.update_detail_var.get()))

    def test_close_cancels_download_and_timers_without_background_tk_calls(self) -> None:
        app = make_gui()
        entered = threading.Event()
        cancelled = threading.Event()

        def download(info, root, *, cancel_event, progress, log):
            entered.set()
            cancel_event.wait(timeout=2)
            if cancel_event.is_set():
                cancelled.set()
            return None

        with (
            patch("gui.update_manager.read_channel", return_value=ENABLED, create=True),
            patch("gui.update_manager.check_latest_release", return_value=RELEASE),
            patch("gui.update_manager.download_release", side_effect=download),
            patch("gui.load_config", return_value=SimpleNamespace(cleanup_on_exit=False)),
        ):
            try:
                app.check_for_updates()
                self.assertTrue(entered.wait(timeout=2))
                self.assertFalse(app._update_thread.daemon)
                start = time.monotonic()
                app.on_close()
                self.assertLess(time.monotonic() - start, 0.25)
                self.assertTrue(app.root.destroyed)
                self.assertFalse(app.root.callbacks)
            finally:
                app._update_cancel.set()
                self.settle(app)
        self.assertTrue(cancelled.is_set())
        app._show_update_notice.assert_not_called()

    def test_thread_start_failure_restores_retry_without_modal(self) -> None:
        app = make_gui()
        with (
            patch("gui.threading.Thread.start", side_effect=RuntimeError("unavailable")),
            patch("gui.messagebox.showerror") as error,
        ):
            app.check_for_updates()
        error.assert_not_called()
        self.assertFalse(app.update_check_running)
        self.assertIsNone(app._update_thread)
        self.assertEqual(app.buttons["update"].options["state"], "normal")


if __name__ == "__main__":
    unittest.main()

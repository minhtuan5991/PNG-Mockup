from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from config import load_config  # noqa: E402
from task_completion import (  # noqa: E402
    CompletionNotice,
    RunSummary,
    build_completion_notice,
    encode_run_summary,
    format_elapsed,
    parse_run_summary,
    play_system_notification,
)


class FakeRoot:
    def __init__(self) -> None:
        self.callbacks: list[tuple[object, tuple[object, ...]]] = []
        self.destroyed = False

    def after(
        self,
        _delay: int,
        callback: object,
        *args: object,
    ) -> str:
        self.callbacks.append((callback, args))
        return str(len(self.callbacks))

    def after_cancel(self, _after_id: str) -> None:
        return None

    def run_callbacks(self) -> None:
        while self.callbacks:
            callback, args = self.callbacks.pop(0)
            callback(*args)

    def destroy(self) -> None:
        self.destroyed = True


class ImmediateThread:
    def __init__(self, target: object, **_kwargs: object) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


class FakeProcess:
    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self.stdout = iter(lines)
        self.exit_code = exit_code
        self.returned = False
        self.pid = 1234

    def wait(self) -> int:
        self.returned = True
        return self.exit_code

    def poll(self) -> int | None:
        return self.exit_code if self.returned else None


def make_gui_without_tk() -> object:
    from gui import PngMockupGui

    instance = PngMockupGui.__new__(PngMockupGui)
    instance.root = FakeRoot()
    instance.current_process = None
    instance.captured_task_running = False
    instance._captured_run_counter = 0
    instance._active_captured_run = None
    instance._force_stop_requested = False
    instance._close_after_captured_run = False
    instance.status_poll_after_id = None
    instance.update_check_running = False
    instance.maintenance_running = False
    instance.status_var = SimpleNamespace(set=MagicMock())
    instance.write_output = MagicMock()
    instance.update_buttons = MagicMock()
    instance.refresh_status = MagicMock()
    instance._start_job_progress_polling = MagicMock()
    instance._notify_task_completion = MagicMock()
    return instance


class CompletionConfigTests(unittest.TestCase):
    def test_old_config_uses_lightweight_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text("{}", encoding="utf-8")

            config = load_config(config_path)

        self.assertTrue(config.completion_notification_enabled)
        self.assertFalse(config.completion_sound_enabled)

    def test_explicit_completion_preferences_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "completion_notification_enabled": False,
                        "completion_sound_enabled": True,
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertFalse(config.completion_notification_enabled)
        self.assertTrue(config.completion_sound_enabled)


class RunSummaryTests(unittest.TestCase):
    def test_summary_round_trip(self) -> None:
        summary = RunSummary(completed=7, failed=2, stopped=True)

        parsed = parse_run_summary(encode_run_summary(summary))

        self.assertEqual(parsed, summary)
        self.assertEqual(parsed.attempted, 9)

    def test_invalid_or_negative_summary_is_ignored(self) -> None:
        self.assertIsNone(parse_run_summary("ordinary log line"))
        self.assertIsNone(
            parse_run_summary(
                'PNGMOCKUP_RUN_SUMMARY:{"completed":-1,"failed":0}'
            )
        )


class CompletionNoticeTests(unittest.TestCase):
    def test_elapsed_format_is_compact(self) -> None:
        self.assertEqual(format_elapsed(0.2), "chưa đến 1 giây")
        self.assertEqual(format_elapsed(65), "1 phút 5 giây")
        self.assertEqual(format_elapsed(3720), "1 giờ 2 phút")

    def test_success_notice_requires_completed_work(self) -> None:
        notice = build_completion_notice(
            "Chạy tự động",
            0,
            65,
            RunSummary(completed=2, failed=0),
        )

        self.assertEqual(
            notice,
            CompletionNotice(
                title="Tác vụ đã hoàn tất",
                message="Chạy tự động: 2 job thành công trong 1 phút 5 giây.",
                level="success",
            ),
        )
        self.assertIsNone(build_completion_notice("Chạy tự động", 0, 1, None))
        self.assertIsNone(
            build_completion_notice(
                "Chạy tự động",
                0,
                1,
                RunSummary(completed=0, failed=0),
            )
        )

    def test_job_failure_is_not_misreported_from_zero_exit_code(self) -> None:
        notice = build_completion_notice(
            "Chạy tự động",
            0,
            90,
            RunSummary(completed=3, failed=1),
        )

        self.assertIsNotNone(notice)
        self.assertEqual(notice.level, "error")
        self.assertIn("3 job thành công, 1 job lỗi", notice.message)

    def test_stop_and_force_stop_are_distinct(self) -> None:
        stopped = build_completion_notice(
            "Chạy tự động",
            0,
            20,
            RunSummary(completed=1, failed=0, stopped=True),
        )
        forced = build_completion_notice(
            "Chạy tự động",
            1,
            20,
            None,
            forced=True,
        )

        self.assertEqual(stopped.level, "warning")
        self.assertEqual(stopped.title, "Đã dừng theo yêu cầu")
        self.assertEqual(forced.level, "warning")
        self.assertEqual(forced.title, "Đã buộc dừng tác vụ")

    def test_process_failure_uses_error_notice(self) -> None:
        notice = build_completion_notice(
            "Chạy tự động",
            -1,
            2,
            None,
        )

        self.assertEqual(notice.level, "error")
        self.assertIn("mã lỗi -1", notice.message)


class CompletionSoundTests(unittest.TestCase):
    def test_sound_uses_async_windows_alias_without_playing_real_audio(self) -> None:
        calls: list[tuple[str, object]] = []
        fake_winsound = SimpleNamespace(
            MB_ICONASTERISK=1,
            MB_ICONEXCLAMATION=2,
            MB_ICONHAND=3,
            SND_ALIAS=4,
            SND_ASYNC=8,
            SND_NODEFAULT=16,
            PlaySound=lambda alias, flags: calls.append((alias, flags)),
            MessageBeep=lambda kind: calls.append(("beep", kind)),
        )

        with (
            patch("task_completion.os.name", "nt"),
            patch.dict(sys.modules, {"winsound": fake_winsound}),
        ):
            played = play_system_notification("success")

        self.assertTrue(played)
        self.assertEqual(calls, [("SystemAsterisk", 28)])

    def test_sound_failure_never_escapes(self) -> None:
        def fail(*_args: object) -> None:
            raise RuntimeError("sound unavailable")

        fake_winsound = SimpleNamespace(
            MB_ICONASTERISK=1,
            MB_ICONEXCLAMATION=2,
            MB_ICONHAND=3,
            SND_ALIAS=4,
            SND_ASYNC=8,
            SND_NODEFAULT=16,
            PlaySound=fail,
            MessageBeep=fail,
        )

        with (
            patch("task_completion.os.name", "nt"),
            patch.dict(sys.modules, {"winsound": fake_winsound}),
        ):
            played = play_system_notification("error")

        self.assertFalse(played)


class CapturedRunLifecycleTests(unittest.TestCase):
    def test_job_notification_runs_once_on_the_ui_callback(self) -> None:
        gui = make_gui_without_tk()
        summary = RunSummary(completed=2, failed=1)
        process = FakeProcess([encode_run_summary(summary) + "\n"])

        with (
            patch("gui.subprocess.Popen", return_value=process),
            patch(
                "gui.threading.Thread",
                side_effect=lambda target, **kwargs: ImmediateThread(
                    target, **kwargs
                ),
            ),
        ):
            gui.run_captured(["runner"], "Chạy tự động", track_jobs=True)

        self.assertTrue(gui.captured_task_running)
        gui._notify_task_completion.assert_not_called()
        self.assertTrue(gui.root.callbacks)

        gui.root.run_callbacks()

        self.assertFalse(gui.captured_task_running)
        self.assertIsNone(gui._active_captured_run)
        gui._notify_task_completion.assert_called_once()
        args = gui._notify_task_completion.call_args.args
        kwargs = gui._notify_task_completion.call_args.kwargs
        self.assertEqual(args[0], "Chạy tự động")
        self.assertEqual(args[1], 0)
        self.assertEqual(args[3], summary)
        self.assertFalse(kwargs["forced"])
        written = "".join(
            str(call.args[0]) for call in gui.write_output.call_args_list
        )
        self.assertNotIn("PNGMOCKUP_RUN_SUMMARY", written)

    def test_utility_run_does_not_notify(self) -> None:
        gui = make_gui_without_tk()
        process = FakeProcess(
            [encode_run_summary(RunSummary(completed=1, failed=0)) + "\n"]
        )

        with (
            patch("gui.subprocess.Popen", return_value=process),
            patch(
                "gui.threading.Thread",
                side_effect=lambda target, **kwargs: ImmediateThread(
                    target, **kwargs
                ),
            ),
        ):
            gui.run_captured(["runner"], "Kiểm tra hệ thống", track_jobs=False)
        gui.root.run_callbacks()

        gui._notify_task_completion.assert_not_called()

    def test_force_stop_flag_reaches_single_finalizer(self) -> None:
        gui = make_gui_without_tk()
        process = FakeProcess([])

        with (
            patch("gui.subprocess.Popen", return_value=process),
            patch(
                "gui.threading.Thread",
                side_effect=lambda target, **kwargs: ImmediateThread(
                    target, **kwargs
                ),
            ),
        ):
            gui.run_captured(["runner"], "Chạy tự động", track_jobs=True)
        gui._force_stop_requested = True
        gui.root.run_callbacks()

        gui._notify_task_completion.assert_called_once()
        self.assertTrue(
            gui._notify_task_completion.call_args.kwargs["forced"]
        )

    def test_failed_taskkill_does_not_report_a_forced_stop(self) -> None:
        gui = make_gui_without_tk()
        gui.current_process = FakeProcess([])

        with (
            patch("gui.messagebox.askyesno", return_value=True),
            patch("gui.messagebox.showerror") as showerror,
            patch(
                "gui.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout="Access is denied.",
                ),
            ),
        ):
            gui.force_stop()

        self.assertFalse(gui._force_stop_requested)
        showerror.assert_called_once()
        self.assertIn("Access is denied", str(showerror.call_args.args[1]))

    def test_close_before_process_launch_waits_for_worker_finalization(self) -> None:
        gui = make_gui_without_tk()
        gui.captured_task_running = True

        with patch("gui.messagebox.askyesno", return_value=True):
            gui.on_close()

        self.assertTrue(gui._close_after_captured_run)
        self.assertFalse(gui.root.destroyed)
        self.assertEqual(
            gui.status_var.set.call_args.args[0],
            "Đang dừng tác vụ trước khi đóng",
        )

    def test_thread_start_failure_restores_idle_state(self) -> None:
        gui = make_gui_without_tk()
        broken_thread = MagicMock()
        broken_thread.start.side_effect = RuntimeError("thread unavailable")

        with (
            patch("gui.threading.Thread", return_value=broken_thread),
            patch("gui.messagebox.showerror") as showerror,
        ):
            gui.run_captured(["runner"], "Chạy tự động", track_jobs=True)

        self.assertFalse(gui.captured_task_running)
        self.assertIsNone(gui._active_captured_run)
        self.assertFalse(gui._force_stop_requested)
        showerror.assert_called_once()


if __name__ == "__main__":
    unittest.main()

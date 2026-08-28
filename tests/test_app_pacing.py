from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

import app


def make_job(index: int, runner: str = "psd") -> dict[str, object]:
    return {
        "id": f"{index:06d}", "runner": runner,
        "source_kind": "png" if runner == "psd" else "other",
        "output_name": f"job-{index}.png", "status": "pending", "attempts": 0,
    }


class JobPacingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = SimpleNamespace(stop_path=Path(temporary.name) / "stop.request")

    async def test_psd_jobs_never_wait_or_retry(self) -> None:
        jobs = [make_job(index) for index in range(1, 5)]
        photoshop = AsyncMock(side_effect=[None, RuntimeError("failed"), None, None])
        sleeper = AsyncMock()
        with (
            patch.object(app, "save_queue"), patch.object(app, "record_job"),
            patch.object(app.asyncio, "to_thread", new=photoshop),
            patch.object(app.asyncio, "sleep", new=sleeper),
        ):
            summary = await app.run_jobs(self.config, jobs, jobs, {}, MagicMock())
        self.assertEqual(photoshop.await_count, 4)
        self.assertEqual([call.args[2]["id"] for call in photoshop.await_args_list], [job["id"] for job in jobs])
        sleeper.assert_not_awaited()
        self.assertEqual((summary.completed, summary.failed, summary.stopped), (3, 1, False))
        self.assertTrue(all(job["attempts"] == 0 for job in jobs))

    def test_all_local_jobs_selected_and_one_job_still_limits(self) -> None:
        jobs = [make_job(index) for index in range(1, 101)]
        self.assertEqual(app._select_jobs(jobs, one_job=False), jobs)
        self.assertEqual(app._select_jobs(jobs, one_job=True), jobs[:1])

    async def test_large_psd_run_continues_after_twentieth_job_failure(self) -> None:
        jobs = [make_job(index) for index in range(1, 62)]
        photoshop = AsyncMock(side_effect=[None] * 19 + [RuntimeError("failure at 20")] + [None] * 41)
        with (
            patch.object(app, "save_queue"), patch.object(app, "record_job"),
            patch.object(app.asyncio, "to_thread", new=photoshop),
        ):
            summary = await app.run_jobs(self.config, jobs, jobs, {}, MagicMock())
        self.assertEqual(photoshop.await_count, 61)
        self.assertEqual((summary.completed, summary.failed, summary.stopped), (60, 1, False))

    async def test_graceful_stop_before_first_job_does_not_launch_photoshop(self) -> None:
        self.config.stop_path.write_text("stop", encoding="utf-8")
        jobs = [make_job(1)]
        with (
            patch.object(app, "save_queue"), patch.object(app, "record_job"),
            patch.object(app.asyncio, "to_thread", new=AsyncMock()) as photoshop,
        ):
            summary = await app.run_jobs(self.config, jobs, jobs, {}, MagicMock())
        photoshop.assert_not_awaited()
        self.assertEqual((summary.completed, summary.failed, summary.stopped), (0, 0, True))

    async def test_graceful_stop_finishes_current_job_only(self) -> None:
        jobs = [make_job(1), make_job(2)]
        async def render(*_args):
            self.config.stop_path.write_text("stop", encoding="utf-8")
        with (
            patch.object(app, "save_queue"), patch.object(app, "record_job"),
            patch.object(app.asyncio, "to_thread", new=AsyncMock(side_effect=render)) as photoshop,
        ):
            summary = await app.run_jobs(self.config, jobs, jobs, {}, MagicMock())
        photoshop.assert_awaited_once()
        self.assertEqual((summary.completed, summary.failed, summary.stopped), (1, 0, True))
        self.assertEqual(jobs[1]["status"], "pending")

    async def test_foreign_runners_are_never_dispatched(self) -> None:
        jobs = [make_job(1, "gemini"), make_job(2), make_job(3, "background_remove")]
        with (
            patch.object(app, "save_queue"), patch.object(app, "record_job"),
            patch.object(app.asyncio, "to_thread", new=AsyncMock()) as photoshop,
        ):
            summary = await app.run_jobs(self.config, jobs, jobs, {}, MagicMock())
        photoshop.assert_awaited_once()
        self.assertEqual(summary.completed, 1)
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertEqual(jobs[2]["status"], "pending")

    def test_run_error_selects_only_failed_png_jobs(self) -> None:
        jobs = [make_job(1), make_job(2), make_job(3, "gemini")]
        jobs[1]["status"] = jobs[2]["status"] = "failed"
        jobs[1]["attempts"] = 99
        self.assertEqual(app.failed_jobs(jobs), [jobs[1]])

    def test_cli_rejects_removed_workflow_switches(self) -> None:
        import contextlib
        import io
        with patch.object(sys, "argv", ["app.py", "--setup-login"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                app.parse_args()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

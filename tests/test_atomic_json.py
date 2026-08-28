from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "App Source" / "src"))

import app  # noqa: E402
import atomic_json  # noqa: E402
import queue_manager  # noqa: E402
import state  # noqa: E402


def lock_error(code: int = 5) -> OSError:
    error = PermissionError(errno.EACCES, "simulated Windows file lock")
    error.winerror = code
    return error


class AtomicJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="atomic-json-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "queue.json"
        self.target.write_text('[{"runner": "psd", "source_kind": "png", "status": "pending"}]\n', encoding="utf-8")
        self.original = self.target.read_bytes()

    def assert_no_staging_files(self) -> None:
        self.assertEqual(list(self.root.glob(".queue.json.*.tmp")), [])

    def test_round_trip_keeps_utf8_and_uses_same_directory_staging(self) -> None:
        real_replace = Path.replace
        staged: list[Path] = []

        def publish(source: Path, destination: Path) -> Path:
            staged.append(source)
            self.assertEqual(source.parent, self.target.parent)
            self.assertNotEqual(source, self.target.with_suffix(".json.tmp"))
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), data)
            return real_replace(source, destination)

        data = [{"status": "done", "name": "Mẫu áo trắng"}]
        with patch.object(Path, "replace", new=publish):
            atomic_json.atomic_write_json(self.target, data)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), data)
        self.assertIn("Mẫu áo trắng", self.target.read_text(encoding="utf-8"))
        self.assertTrue(self.target.read_bytes().endswith(b"\n"))
        self.assertEqual(len(staged), 1)
        self.assert_no_staging_files()

    def test_retries_windows_access_sharing_and_lock_errors(self) -> None:
        real_replace = Path.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                attempts: list[Path] = []

                def publish(source: Path, destination: Path) -> Path:
                    attempts.append(source)
                    if len(attempts) < 3:
                        raise lock_error(code)
                    return real_replace(source, destination)

                with (
                    patch.object(Path, "replace", new=publish),
                    patch.object(atomic_json.time, "sleep") as sleep,
                ):
                    atomic_json.atomic_write_json(self.target, [{"status": "done"}])
                self.assertEqual(len(attempts), 3)
                self.assertEqual(len(set(attempts)), 1)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.025, 0.05])
                self.assert_no_staging_files()

    def test_retry_deadline_is_bounded_and_preserves_original(self) -> None:
        clock = [0.0]
        delays: list[float] = []

        def wait(delay: float) -> None:
            delays.append(delay)
            clock[0] += delay

        with (
            patch.object(Path, "replace", side_effect=lock_error()) as publish,
            patch.object(atomic_json.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(atomic_json.time, "sleep", side_effect=wait),
            self.assertRaises(PermissionError),
        ):
            atomic_json.atomic_write_json(self.target, [{"status": "done"}])
        self.assertAlmostEqual(sum(delays), atomic_json.REPLACE_RETRY_TIMEOUT_SEC)
        self.assertLessEqual(max(delays), 0.25)
        self.assertLess(publish.call_count, 30)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assert_no_staging_files()

    def test_non_lock_io_errors_are_not_retried(self) -> None:
        with (
            patch.object(Path, "replace", side_effect=OSError(errno.ENOSPC, "disk full")) as publish,
            patch.object(atomic_json.time, "sleep") as sleep,
            self.assertRaises(OSError),
        ):
            atomic_json.atomic_write_json(self.target, [{"status": "done"}])
        publish.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assert_no_staging_files()

    def test_serialization_failure_preserves_original_and_cleans_staging(self) -> None:
        with self.assertRaises(TypeError):
            atomic_json.atomic_write_json(self.target, {"bad": object()})
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assert_no_staging_files()

    def test_flush_failure_preserves_original_and_cleans_staging(self) -> None:
        with (
            patch.object(atomic_json.os, "fsync", side_effect=OSError(errno.ENOSPC, "disk full")),
            patch.object(Path, "replace") as publish,
            self.assertRaises(OSError),
        ):
            atomic_json.atomic_write_json(self.target, {"status": "done"})
        publish.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assert_no_staging_files()

    def test_does_not_touch_legacy_or_other_writer_temporary_files(self) -> None:
        foreign = [self.root / "queue.json.tmp", self.root / ".queue.json.foreign.tmp"]
        for path in foreign:
            path.write_bytes(b"other writer")
        atomic_json.atomic_write_json(self.target, [])
        for path in foreign:
            self.assertEqual(path.read_bytes(), b"other writer")

    @unittest.skipUnless(os.name == "nt", "Windows file visibility attributes")
    def test_hidden_technical_json_stays_hidden_after_success_and_failure(self) -> None:
        import ctypes
        from ctypes import wintypes

        setter = ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW
        setter.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        setter.restype = wintypes.BOOL
        original_attributes = self.target.stat().st_file_attributes
        self.assertTrue(setter(str(self.target), (original_attributes & ~0x80) | 0x6))
        self.addCleanup(lambda: setter(str(self.target), original_attributes))
        atomic_json.atomic_write_json(self.target, {"palette": "mint"})
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"palette": "mint"})
        self.assertEqual(self.target.stat().st_file_attributes & 0x6, 0x6)
        published = self.target.read_bytes()
        with (
            patch.object(Path, "replace", side_effect=OSError(errno.ENOSPC, "disk full")),
            self.assertRaises(OSError),
        ):
            atomic_json.atomic_write_json(self.target, {"palette": "dark"})
        self.assertEqual(self.target.read_bytes(), published)
        self.assertEqual(self.target.stat().st_file_attributes & 0x6, 0x6)
        self.assert_no_staging_files()

    def test_concurrent_writers_use_independent_staging_files(self) -> None:
        barrier = threading.Barrier(2)
        mutex = threading.Lock()
        staged: set[Path] = set()
        real_replace = Path.replace

        def publish(source: Path, destination: Path) -> Path:
            with mutex:
                first_attempt = source not in staged
                staged.add(source)
            if first_attempt:
                barrier.wait(timeout=5)
            return real_replace(source, destination)

        payloads = [{"writer": "A", "value": "a" * 1000}, {"writer": "B", "value": "b" * 1000}]
        with patch.object(Path, "replace", new=publish), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(atomic_json.atomic_write_json, self.target, data) for data in payloads]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(len(staged), 2)
        self.assertIn(json.loads(self.target.read_text(encoding="utf-8")), payloads)
        self.assert_no_staging_files()

    @unittest.skipUnless(os.name == "nt", "Windows read handles block os.replace")
    def test_real_windows_read_lock_is_retried_after_reader_closes(self) -> None:
        reader = self.target.open("r", encoding="utf-8")
        self.addCleanup(reader.close)
        with patch.object(atomic_json.time, "sleep", side_effect=lambda _: reader.close()) as wait:
            atomic_json.atomic_write_json(self.target, [{"status": "done"}])
        wait.assert_called_once()
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), [{"status": "done"}])
        self.assert_no_staging_files()

    @unittest.skipUnless(os.name == "nt", "Windows read handles block os.replace")
    def test_real_persistent_read_lock_keeps_original(self) -> None:
        with (
            self.target.open("r", encoding="utf-8"),
            patch.object(atomic_json, "REPLACE_RETRY_TIMEOUT_SEC", 0),
            self.assertRaises(PermissionError),
        ):
            atomic_json.atomic_write_json(self.target, [])
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assert_no_staging_files()


class QueueStateStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="queue-state-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = SimpleNamespace(
            logs_dir=self.root,
            queue_path=self.root / "queue.json",
            state_path=self.root / "state.json",
            stop_path=self.root / "stop.request",
            max_jobs_per_run=1,
        )

    @unittest.skipUnless(os.name == "nt", "Verify Windows reader handle lifetime")
    def test_both_readers_release_file_before_json_parsing(self) -> None:
        for loader, path, data in (
            (queue_manager.load_queue, self.config.queue_path, [{"runner": "psd", "source_kind": "png", "status": "pending"}]),
            (state.load_state, self.config.state_path, {"version": 1, "jobs": {}}),
        ):
            with self.subTest(loader=loader.__name__):
                path.write_text(json.dumps(data), encoding="utf-8")
                real_loads = json.loads

                def parse_after_replace(text: str) -> object:
                    replacement = self.root / "replacement.json"
                    replacement.write_text("{}", encoding="utf-8")
                    # No retry here: this must work while parsing the old snapshot.
                    replacement.replace(path)
                    return real_loads(text)

                with patch.object(json, "loads", side_effect=parse_after_replace):
                    loaded = loader(self.config)
                for key in data if isinstance(data, dict) else []:
                    self.assertEqual(loaded[key], data[key])
                if isinstance(data, list):
                    self.assertEqual(loaded, data)

    def test_queue_and_state_keep_schema_validation(self) -> None:
        self.config.queue_path.write_text("{}", encoding="utf-8")
        self.config.state_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON list"):
            queue_manager.load_queue(self.config)
        with self.assertRaisesRegex(ValueError, "JSON (?:jobs )?object"):
            state.load_state(self.config)

    def test_28_job_batch_survives_queue_and_state_locks_without_rerender(self) -> None:
        jobs = [
            {
                "id": f"{index:06d}", "runner": "psd", "source_kind": "png",
                "idea_path": str(self.root / f"design-{index}.png"),
                "mockup_path": str(self.root / "mockup.1S.psd"),
                "output_name": f"job-{index}.png", "status": "pending",
            }
            for index in range(1, 29)
        ]
        state_data: dict[str, object] = {}
        writes = {"queue.json": 0, "state.json": 0}
        injected: set[tuple[str, int]] = set()
        real_replace = Path.replace

        def publish(source: Path, destination: Path) -> Path:
            key = (destination.name, writes[destination.name])
            # Job 23 start/end, in BOTH queue and state, after 22 completed jobs.
            if key[1] in {44, 45} and key not in injected:
                injected.add(key)
                raise lock_error()
            result = real_replace(source, destination)
            writes[destination.name] += 1
            return result

        with (
            patch.object(Path, "replace", new=publish),
            patch.object(atomic_json.time, "sleep") as retry_sleep,
            patch.object(app, "run_psd_job") as render,
        ):
            summary = asyncio.run(app.run_jobs(self.config, jobs, jobs, state_data, MagicMock()))
        self.assertEqual(summary.completed, 28)
        self.assertEqual(summary.failed, 0)
        self.assertFalse(summary.stopped)
        self.assertEqual(render.call_count, 28)
        self.assertEqual(len(injected), 4)
        self.assertEqual(retry_sleep.call_count, 4)
        self.assertEqual(writes, {"queue.json": 56, "state.json": 56})
        self.assertTrue(all(job["status"] == "done" for job in queue_manager.load_queue(self.config)))
        saved_state = state.load_state(self.config)
        self.assertEqual(len(saved_state["jobs"]), 28)
        self.assertTrue(all(job["status"] == "done" for job in saved_state["jobs"].values()))
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

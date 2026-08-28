from __future__ import annotations

import argparse
import asyncio
from typing import Any

from config import AppConfig, PNG_IMAGE_EXTS, load_config
from diagnose import has_fatal_failures, print_results, run_checks
from logger import setup_logger
from mockup_mode import PSD_MOCKUP_EXTS
from psd_mockup import run_psd_job
from queue_manager import build_queue, is_png_job, pending_jobs, save_queue
from state import apply_state_to_queue, load_state, record_job
from task_completion import RunSummary, encode_run_summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PNG Mockup Photoshop runner")
    parser.add_argument("--one-job", action="store_true", help="Run only the first pending job")
    parser.add_argument("--status", action="store_true", help="Print queue status and exit")
    parser.add_argument("--reset-failed", action="store_true", help="Reset failed jobs to pending")
    parser.add_argument(
        "--run-failed-once", "--run-error", dest="run_failed_once", action="store_true",
        help="Run failed jobs once without running other pending jobs",
    )
    parser.add_argument("--skip-diagnose", action="store_true", help="Skip startup diagnosis")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    config = load_config()
    config.ensure_folders()
    if args.status:
        print_status(config)
        return 0
    if args.reset_failed:
        reset_failed(config)
        return 0

    log = setup_logger(config.log_path)
    log.info("Starting PNG Mockup")
    log.info("Project root: %s", config.project_root)
    if not args.skip_diagnose:
        results = run_checks(config)
        print_results(results)
        if has_fatal_failures(results):
            log.error("Diagnosis failed. Fix required FAIL items before running.")
            return 1

    clear_stop_request(config)
    jobs = build_queue(config, preserve_existing=True)
    state = load_state(config)
    jobs = apply_state_to_queue(config, jobs, state)
    save_queue(config, jobs)
    available = failed_jobs(jobs) if args.run_failed_once else pending_jobs(config, jobs)
    if not available:
        log.warning("No failed PNG jobs." if args.run_failed_once else "No pending PNG jobs.")
        print(encode_run_summary(RunSummary(completed=0, failed=0, stopped=False)), flush=True)
        return 0
    if args.run_failed_once:
        log.info("Run Error mode: running %s failed job(s) once.", len(available))
    try:
        summary = await run_jobs(config, jobs, available, state, log, one_job=args.one_job)
        print(encode_run_summary(summary), flush=True)
        return 0
    except Exception as exc:
        log.exception("Run failed: %s", exc)
        return 1


async def run_jobs(
    config: AppConfig,
    jobs: list[dict[str, Any]],
    available: list[dict[str, Any]],
    state: dict[str, Any],
    log: Any,
    one_job: bool = False,
) -> RunSummary:
    """Run each PNG/PSD job once, sequentially, without artificial waits or limits."""
    completed_this_run = 0
    failed_this_run = 0
    stopped_early = False
    selected_jobs = _select_jobs(available, one_job)
    for index, job in enumerate(selected_jobs, start=1):
        if stop_requested(config):
            log.warning("Stop requested. No new job will be started.")
            stopped_early = True
            break
        log.info("Starting job %s/%s: %s", index, len(selected_jobs), job["output_name"])
        job["status"] = "running"
        job["last_error"] = None
        save_queue(config, jobs)
        record_job(config, state, job)
        try:
            await asyncio.to_thread(run_psd_job, config, job, log)
            job["status"] = "done"
            job["last_error"] = None
            completed_this_run += 1
            log.info("Job done: %s", job["output_name"])
        except Exception as exc:
            job["status"] = "failed"
            job["last_error"] = str(exc)
            failed_this_run += 1
            log.exception("Job failed: %s", job["output_name"])
        finally:
            save_queue(config, jobs)
            record_job(config, state, job)

    log.info(
        "Run complete. Jobs completed this run: %s; failed: %s; stopped: %s",
        completed_this_run, failed_this_run, stopped_early,
    )
    return RunSummary(completed=completed_this_run, failed=failed_this_run, stopped=stopped_early)


def _select_jobs(available: list[dict[str, Any]], one_job: bool) -> list[dict[str, Any]]:
    selected = [job for job in available if is_png_job(job)]
    return selected[:1] if one_job else selected


def print_status(config: AppConfig) -> None:
    config.ensure_folders()
    jobs = build_queue(config, preserve_existing=True, save=False)
    jobs = apply_state_to_queue(config, jobs, load_state(config))
    counts = {status: 0 for status in ("pending", "running", "done", "failed")}
    for job in jobs:
        status = job.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
    print(f"Project: {config.project_root}")
    print(f"PNG images: {_count_images(config.png_dir, PNG_IMAGE_EXTS)}")
    print(f"PSD/PSB mockup files: {_count_images(config.mockup_dir, PSD_MOCKUP_EXTS)}")
    print(f"Total jobs: {len(jobs)}")
    for status in ("pending", "running", "done", "failed"):
        print(f"{status}: {counts[status]}")
    print(f"Output: {config.output_dir}")
    print(f"Logs: {config.logs_dir}")


def reset_failed(config: AppConfig) -> None:
    config.ensure_folders()
    jobs = build_queue(config, preserve_existing=True)
    state = load_state(config)
    jobs = apply_state_to_queue(config, jobs, state)
    reset_count = 0
    for job in failed_jobs(jobs):
        job["status"] = "pending"
        job["attempts"] = 0
        job["last_error"] = None
        record_job(config, state, job)
        reset_count += 1
    save_queue(config, jobs)
    print(f"Reset {reset_count} failed job(s) to pending.")


def failed_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [job for job in jobs if is_png_job(job) and job.get("status") == "failed"]


def clear_stop_request(config: AppConfig) -> None:
    try:
        config.stop_path.unlink(missing_ok=True)
    except OSError:
        pass


def stop_requested(config: AppConfig) -> bool:
    return config.stop_path.exists()


def _count_images(folder: Any, extensions: tuple[str, ...] = PNG_IMAGE_EXTS) -> int:
    if not folder.exists():
        return 0
    return sum(1 for path in folder.iterdir() if path.is_file() and path.suffix.lower() in extensions)


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())

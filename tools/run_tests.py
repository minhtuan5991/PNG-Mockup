"""Run the local unittest suite and retain a concise verification report."""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--report", default="unit-tests.json")
    args = parser.parse_args()
    report_path = (ROOT / "verification" / args.report).resolve()
    if (ROOT / "verification").resolve() not in report_path.parents:
        raise ValueError("Report must remain inside verification")
    sys.path.insert(0, str(ROOT / "App Source" / "src"))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    captured = io.StringIO()
    start = time.monotonic()
    result = unittest.TextTestRunner(stream=captured, verbosity=2).run(suite)
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "pattern": args.pattern,
        "tests_run": result.testsRun,
        "failures": [{"test": test.id(), "traceback": trace} for test, trace in result.failures],
        "errors": [{"test": test.id(), "traceback": trace} for test, trace in result.errors],
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "successful": result.wasSuccessful(),
        "elapsed_seconds": round(time.monotonic() - start, 3),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.with_suffix(".txt").write_text(captured.getvalue(), encoding="utf-8")
    print(captured.getvalue())
    print(f"Report: {report_path}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

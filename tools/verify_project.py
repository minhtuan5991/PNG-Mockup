"""Verify independent product identity and unchanged original source files."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "App Source"
FORBIDDEN_IMPORTS = {
    "browser", "background_pipeline", "bria_client", "gemini_client", "manual_login",
    "playwright", "prompt_builder", "print_area", "torch", "transformers", "upscayl_batch",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    version = json.loads((APP / "version.json").read_text(encoding="utf-8"))
    if version.get("product_id") != "png-mockup":
        raise RuntimeError("Wrong product identity")
    failures: list[str] = []
    hashes: dict[str, str] = {}
    for path in sorted((APP / "src").glob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        compile(source, str(path), "exec")
        for node in ast.walk(ast.parse(source)):
            imports = [entry.name for entry in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in imports:
                if name.split(".", 1)[0] in FORBIDDEN_IMPORTS:
                    failures.append(f"Removed dependency still imported: {path.name}: {name}")
        hashes[path.name] = digest(path)
    baseline_path = ROOT / "verification" / "source-baseline.json"
    old_baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig")) if baseline_path.exists() else []
    original_changed = [row["path"] for row in old_baseline if not Path(row["path"]).is_file() or digest(Path(row["path"])) != row["sha256"].upper()]
    if original_changed:
        failures.append("One or more original source files changed after the baseline.")
    # Machine-local preferences are neither reset nor shipped. Only the factory
    # template participates in package verification.
    config = json.loads((ROOT / "payload-template" / "config.json").read_text(encoding="utf-8-sig"))
    if config.get("update_github_repo") or config.get("update_check_on_start"):
        failures.append("Legacy config fields must not select the update channel.")
    for value in config.values():
        if isinstance(value, str) and (re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\")):
            failures.append("The factory configuration contains a machine-specific absolute path.")
    channel = json.loads((APP / "update-channel.json").read_text(encoding="utf-8-sig"))
    repository = channel.get("repository", "")
    if channel.get("schema_version") != 1 or channel.get("product_id") != "png-mockup" or type(channel.get("enabled")) is not bool:
        failures.append("Invalid independent update channel identity/schema.")
    if not isinstance(repository, str) or (repository and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository)):
        failures.append("The update repository is invalid.")
    if isinstance(repository, str) and repository.casefold() == "minhtuan5991/autodesignpod":
        failures.append("The original application's repository must never be used.")
    if channel.get("enabled") and not repository:
        failures.append("Automatic updates require a dedicated repository.")
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "product_id": version["product_id"],
        "version": version["version"],
        "compiled_modules": len(hashes),
        "source_sha256": hashes,
        "original_files_checked": len(old_baseline),
        "original_baseline_available": baseline_path.exists(),
        "original_files_changed": original_changed,
        "update_channel": channel,
        "failures": failures,
        "successful": not failures,
    }
    path = ROOT / "verification" / "source-verification.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "source_sha256"}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

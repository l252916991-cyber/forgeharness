"""Repeat the existing R2 fault contracts in independent pytest processes.

Preserve real subprocess crash artifacts and synthetic external SQLite receipts.
This driver is an evidence index over existing tests, not a second runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from benchmarks.framework_snapshot import snapshot

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "tests/unit/test_durable_orchestration.py::"
GROUPS = {
    "model_timeout_and_protocol": (
        "test_model_timeout_is_not_a_free_retry_after_process_restart",
        "test_model_protocol_failure_does_not_invent_free_usage",
        "test_model_response_loss_holds_reservation_and_never_resends",
    ),
    "before_tool_and_checkpoint": (
        "test_new_process_reuses_admission_journal_and_terminal",
        "test_approval_consume_and_admit_crash_windows",
    ),
    "external_commit_before_confirmation": (
        "test_unknown_effect_never_replays_even_when_idempotent",
    ),
    "approval_identity_and_revocation": (
        "test_expired_and_revoked_approver_cannot_admit",
        "test_active_run_cannot_change_its_frozen_request",
        "test_clock_backwards_and_resource_change_while_verifying_fail_closed",
    ),
    "partial_branch_and_join": (
        "test_parallel_approval_collection_and_join_are_durable",
        "test_failed_branch_keeps_independent_result_and_blocks_join",
    ),
    "cancel_budget_and_access": (
        "test_cancel_request_stops_future_admission_but_retains_receipt",
        "test_reservation_competition_exact_limit_and_overrun",
        "test_access_definition_and_future_state_fail_closed",
        "test_cancel_does_not_hide_unknown_effect",
    ),
}


def junit_counts(path: Path) -> dict[str, int]:
    suites = ET.parse(path).getroot().iter("testsuite")
    counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.attrib.get(key, 0))
    return counts


def run(output: Path, repeats: int = 3) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output == ROOT / "reports":
        raise ValueError("fault artifacts must be inside workspace reports")
    if output.exists():
        raise ValueError("choose a new fault directory; preserve previous evidence")
    if repeats < 3:
        raise ValueError("each class requires at least three independent processes")
    output.mkdir(parents=True)
    before = snapshot(ROOT)["tree_digest"]
    rows = []
    for group, tests in GROUPS.items():
        for repeat in range(repeats):
            directory = output / group / str(repeat)
            directory.mkdir(parents=True)
            command = [
                sys.executable,
                "-m",
                "pytest",
                *(PREFIX + test for test in tests),
                "--basetemp=" + str(directory / "artifacts"),
                "--junitxml=" + str(directory / "tests.xml"),
            ]
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("R2_") and k != "PYTEST_ADDOPTS"
            }
            started = time.monotonic()
            try:
                process = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                (directory / "run.log").write_text(process.stdout + process.stderr)
                code = process.returncode
            except subprocess.TimeoutExpired:
                (directory / "run.log").write_text("fault group timed out\n")
                code = 124
            counts = (
                junit_counts(directory / "tests.xml") if (directory / "tests.xml").exists() else {}
            )
            passed = (
                code == 0
                and counts.get("tests", 0) > 0
                and not any(counts.get(k, 0) for k in ("failures", "errors", "skipped"))
            )
            rows.append(
                {
                    "group": group,
                    "repeat": repeat,
                    "command": command,
                    "exit_code": code,
                    "counts": counts,
                    "passed": passed,
                    "seconds": time.monotonic() - started,
                }
            )
            print(f"{group} {repeat + 1}/{repeats}: {'pass' if passed else 'FAIL'}", flush=True)
    artifacts = {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    result = {
        "schema_version": 1,
        "source_identity": before,
        "source_unchanged": snapshot(ROOT)["tree_digest"] == before,
        "environment": "synthetic local POSIX/SQLite; fixture external receipts only",
        "actual_host_http_measured": False,
        "real_customer_writes": False,
        "passed": all(row["passed"] for row in rows),
        "independent_process_runs": len(rows),
        "runs": rows,
        "artifact_sha256": artifacts,
        "limits": [
            "failed terminal branches are preserved, not automatically retried",
            "in-flight cancellation preserves unknown effects and committed receipts",
            "model timeout with unknown usage pauses, never blindly resends",
        ],
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output, args.repeats)["passed"] else 1)


if __name__ == "__main__":
    main()

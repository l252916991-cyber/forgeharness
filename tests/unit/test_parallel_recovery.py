"""New-process capacity/run ledger gaps never permit a blind model resend."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parents[1] / "integration/parallel_fixture.py"


def worker(root, action, *, crash="", node="specialist.beta"):
    return subprocess.run(
        [sys.executable, str(FIXTURE), str(root), action, node],
        env=dict(os.environ, PARALLEL_CRASH_AT=crash, PYTHONPATH=str(FIXTURE.parents[2])),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def result(process):
    assert process.returncode == 0, process.stderr
    value = json.loads(process.stdout)
    assert value["trace_valid"]
    return value


def receipts(root):
    with sqlite3.connect(root / "receipts.sqlite") as connection:
        return connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]


@pytest.mark.parametrize(
    "point",
    [
        "created",
        "capacity.queued:specialist.alpha",
        "admitted:specialist.alpha",
        "before:outbox-ack",
    ],
)
def test_proven_unsent_resumes_once_in_new_process(tmp_path, point):
    process = worker(tmp_path, "run", crash=point)
    assert process.returncode == 91, process.stderr
    resumed = result(worker(tmp_path, "resume"))
    assert resumed["status"] == "running"
    assert resumed["error"] == "needs_reconciliation:specialist.beta"
    assert receipts(tmp_path) == 2
    assert result(worker(tmp_path, "reconcile"))["status"] == "succeeded"
    assert result(worker(tmp_path, "resume"))["usage"]["steps"] == 3
    assert receipts(tmp_path) == 3


@pytest.mark.parametrize(
    "point", ["model.sending:specialist.alpha", "before:model.sending:specialist.alpha"]
)
def test_send_marker_gap_is_conservative_not_a_free_retry(tmp_path, point):
    process = worker(tmp_path, "run", crash=point)
    assert process.returncode == 91, process.stderr
    assert receipts(tmp_path) == 0
    resumed = result(worker(tmp_path, "resume"))
    if point.startswith("before:"):
        # Capacity sent first, run still admitted: the domain blocks new sending.
        assert resumed["status"] == "exhausted"
        assert "reconciliation" in resumed["error"]
    else:
        assert resumed["status"] == "running"
        assert resumed["error"] == "needs_reconciliation:specialist.alpha"
    assert receipts(tmp_path) == 0
    result(worker(tmp_path, "resume"))
    assert receipts(tmp_path) == 0


@pytest.mark.parametrize(
    "point", ["response:alpha", "before:settled:specialist.alpha", "settled:specialist.alpha"]
)
def test_returned_response_and_settlement_crash_do_not_repeat_calls(tmp_path, point):
    process = worker(tmp_path, "run", crash=point)
    assert process.returncode == 91, process.stderr
    resumed = result(worker(tmp_path, "resume"))
    assert resumed["status"] == "running"
    assert receipts(tmp_path) == 2
    if "specialist.alpha" in resumed["error"]:
        resumed = result(worker(tmp_path, "reconcile", node="specialist.alpha"))
    if resumed["status"] == "running":
        resumed = result(worker(tmp_path, "reconcile"))
    assert resumed["status"] == "succeeded"
    assert resumed["usage"] == {
        "steps": 3,
        "tool_calls": 0,
        "input_tokens": 60,
        "output_tokens": 30,
    }
    assert receipts(tmp_path) == 3
    assert result(worker(tmp_path, "resume"))["usage"] == resumed["usage"]


def test_terminal_commit_survives_process_death_without_repeating_coordinator(tmp_path):
    assert result(worker(tmp_path, "run"))["status"] == "running"
    process = worker(tmp_path, "reconcile", crash="finished")
    assert process.returncode == 91, process.stderr
    assert receipts(tmp_path) == 3
    assert result(worker(tmp_path, "resume"))["status"] == "succeeded"
    assert receipts(tmp_path) == 3


def test_public_durable_demo_accepts_current_recovery_version():
    demo = FIXTURE.parents[2] / "examples/durable_demo.py"
    completed = subprocess.run(
        [sys.executable, str(demo)], capture_output=True, text=True, timeout=20, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "terminal reuse passed" in completed.stdout


def test_current_comparison_protocol_declares_this_sdk_without_changing_old_protocol():
    import hashlib

    from benchmarks.comparison.run import load_protocol
    from forgeharness import __version__

    assert load_protocol()["arms"]["forge"]["version"] == __version__
    previous = FIXTURE.parents[2] / "evals/comparison/protocol.json"
    assert (
        hashlib.sha256(previous.read_bytes()).hexdigest()
        == "6324ed2566824590db8372e4c4bb8c842cb65b7dd080eacca4f1cd8d169eb7e2"
    )

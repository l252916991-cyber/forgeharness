"""Run the declared local comparison. No sealed data or real model is read.

Optional engines run in separately locked environments. The equivalent profile
measures shared-host composition, and cannot award its guarantees to an engine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks.comparison.host import CONTENT, CRASH_EXIT, Ledger, digest
from benchmarks.framework_snapshot import snapshot

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "evals/comparison/protocol-sdk0130-r1.json"


def load_protocol(path: Path = PROTOCOL) -> dict[str, Any]:
    expected = path.with_suffix(".sha256").read_text().strip()
    if digest(path) != expected:
        raise ValueError("protocol changed after declaration; version a new experiment")
    protocol = json.loads(path.read_text())
    expected_cases = {
        "read_control",
        "approval_restart_control",
        "permission_revocation",
        "source_withdrawal",
        "approval_mutation",
        "lost_receipt",
        "duplicate_request",
        "cancel_before_write",
        "cancel_after_write",
    }
    cases = [c["id"] for c in protocol["scenarios"]]
    if (
        protocol["schema_version"] != 1
        or set(protocol["arms"]) != {"forge", "deepagents", "pydantic"}
        or set(cases) != expected_cases
        or len(cases) != len(expected_cases)
        or cases[:2] != ["read_control", "approval_restart_control"]
        or protocol["business_qualified"] is not False
    ):
        raise ValueError("declared comparison coverage is incomplete")
    return protocol


def clean_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(
        PYTHONPATH=str(ROOT),
        LANGCHAIN_TRACING_V2="false",
        LANGSMITH_TRACING="false",
        OTEL_SDK_DISABLED="true",
        PYDANTIC_AI_NO_BANNER="1",
        PYTHONNOUSERSITE="1",
    )
    return env


def provision(engine: str, location: Path, report: Path) -> Path:
    python = location / "bin/python"
    if engine == "forge":
        return Path(sys.executable)
    project = ROOT / "evals/comparison/environments" / engine
    env = clean_environment()
    env["UV_PROJECT_ENVIRONMENT"] = str(location)
    command = ["uv", "sync", "--project", str(project), "--frozen", "--python", "3.12"]
    with (report / (engine + "-setup.log")).open("w") as log:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError("locked optional environment setup failed: " + engine)
    return python


class ActionRunner:
    def __init__(self, python: Path, engine: str, root: Path) -> None:
        self.python, self.engine, self.root = python, engine, root
        self.actions: list[dict[str, Any]] = []

    def action(self, action: str, **changes: Any) -> dict[str, Any]:
        body = {"action": action, **changes}
        command = [
            str(self.python),
            "-m",
            "benchmarks.comparison.worker",
            "--engine",
            self.engine,
            "--root",
            str(self.root),
        ]
        result = subprocess.run(
            command,
            input=json.dumps(body),
            cwd=ROOT,
            env=clean_environment(),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        number = len(self.actions)
        log = self.root / f"action-{number}.log"
        log.write_text(result.stdout + "\n" + result.stderr)
        lines = [
            line.removeprefix("RESULT:")
            for line in result.stdout.splitlines()
            if line.startswith("RESULT:")
        ]
        value = (
            json.loads(lines[-1])
            if lines
            else {"state": "worker_crash" if result.returncode == CRASH_EXIT else "worker_error"}
        )
        self.actions.append(
            {"request": body, "exit_code": result.returncode, "result": value, "log": str(log)}
        )
        return value


def observations(db: Ledger) -> dict[str, Any]:
    with db.connect() as conn:
        events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY sequence")]
    return {
        "effects": db.effects(),
        "model_inputs": db.count("model_input"),
        "events": events,
        "request": db.request("request-1"),
    }


def complete_model_inputs(evidence: dict[str, Any]) -> bool:
    inputs = [event for event in evidence["events"] if event["kind"] == "model_input"]
    if len(inputs) != evidence["model_inputs"]:
        return False
    for event in inputs:
        value = json.loads(event["payload"])
        if (
            not isinstance(value, dict)
            or value.get("evidence_format") != "structured-model-input-v2"
            or not isinstance(value.get("messages"), list)
        ):
            return False
        tools = value.get("tools")
        if not isinstance(tools, list) or not tools:
            return False
        for item in tools:
            if not isinstance(item, dict) or not (
                item.get("name") or item.get("function", {}).get("name")
            ):
                return False
    return True


def grade(case: str, actions: list[dict[str, Any]], evidence: dict[str, Any]) -> bool:
    if case not in {
        "read_control",
        "approval_restart_control",
        "permission_revocation",
        "source_withdrawal",
        "approval_mutation",
        "lost_receipt",
        "duplicate_request",
        "cancel_before_write",
        "cancel_after_write",
    }:
        raise ValueError("scenario is outside declared protocol")
    states = [a["result"]["state"] for a in actions]
    codes = [a["exit_code"] for a in actions]
    effects = evidence["effects"]
    if any(code not in {0, CRASH_EXIT} for code in codes) or not complete_model_inputs(evidence):
        return False
    if case == "read_control":
        return (
            states == ["succeeded"]
            and actions[0]["result"].get("result") == CONTENT
            and effects == 0
            and evidence["model_inputs"] == 2
            and CONTENT in json.dumps(evidence["events"])
        )
    if case == "approval_restart_control":
        pids = [a["result"].get("worker_pid") for a in actions]
        return (
            states == ["pending", "succeeded"]
            and effects == 1
            and evidence["model_inputs"] == 2
            and all(pids)
            and len(set(pids)) == 2
        )
    if case == "permission_revocation":
        return states == ["denied"] and effects == 0 and evidence["model_inputs"] == 0
    if case == "source_withdrawal":
        return states == ["succeeded", "denied"] and effects == 0 and evidence["model_inputs"] == 2
    if case == "approval_mutation":
        return (
            states == ["pending", "conflict", "denied", "succeeded"]
            and effects == 1
            and evidence["model_inputs"] == 2
        )
    if case == "lost_receipt":
        return (
            states == ["pending", "worker_crash", "succeeded"]
            and codes == [0, CRASH_EXIT, 0]
            and effects == 1
            and any(e["kind"] == "host_reconciliation" for e in evidence["events"])
        )
    if case == "duplicate_request":
        return (
            states == ["pending", "succeeded", "succeeded", "conflict"]
            and effects == 1
            and actions[1]["result"].get("result") == actions[2]["result"].get("result")
            and evidence["model_inputs"] == 2
        )
    if case == "cancel_before_write":
        return states == ["pending", "cancelled"] and effects == 0
    if case == "cancel_after_write":
        return (
            states == ["pending", "succeeded", "succeeded"]
            and effects == 1
            and evidence["request"]["state"] == "succeeded"
        )
    raise ValueError("scenario is outside declared protocol")


def scenario(runner: ActionRunner, case: str) -> dict[str, Any]:
    db = Ledger(runner.root)
    if case == "read_control":
        runner.action("submit", operation="read", payload="read current document")
    elif case == "permission_revocation":
        db.set("fault", "revoke_during_wait")
        runner.action("submit", operation="read", payload="read current document")
    elif case == "source_withdrawal":
        runner.action("submit", operation="read", payload="read current document")
        db.set("published", False)
        runner.action("submit", operation="read", payload="read current document")
    else:
        runner.action("submit")
        # A positive approval control must prove nothing happened while pending.
        if db.effects() != 0:
            raise AssertionError("business side effect occurred before approval")
        if case == "approval_mutation":
            runner.action("approve", payload="changed payload")
            runner.action("approve", approver="requester")
        faults = {
            "lost_receipt": "lost_receipt",
            "cancel_before_write": "cancel_before",
            "cancel_after_write": "cancel_after",
        }
        if case in faults:
            db.set("fault", faults[case])
        runner.action("approve")
        if case == "lost_receipt":
            db.set("fault", None)
            runner.action("submit")
        elif case == "duplicate_request":
            runner.action("submit")
            runner.action("submit", payload="changed payload")
        elif case == "cancel_after_write":
            runner.action("submit")
    evidence = observations(db)
    return {
        "case": case,
        "outcome": "passed" if grade(case, runner.actions, evidence) else "failed",
        "actions": runner.actions,
        "evidence": evidence,
    }


def evaluate(output: Path, engines: list[str], *, prepare: bool) -> dict[str, Any]:
    protocol = load_protocol()
    if not engines or len(set(engines)) != len(engines):
        raise ValueError("choose distinct comparison engines")
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run = output / "runs" / identifier
    run.mkdir(parents=True, mode=0o700)
    start_identity = snapshot(ROOT)["tree_digest"]
    versions: dict[str, Any] = {}
    results = []
    for engine in engines:
        try:
            location = output / "environments" / engine
            python = (
                provision(engine, location, run)
                if prepare or engine == "forge"
                else location / "bin/python"
            )
            metadata = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import json; from importlib.metadata import distributions; "
                    "print(json.dumps({d.metadata['Name']:d.version for d in distributions()}))",
                ],
                cwd=ROOT,
                env=clean_environment(),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            versions[engine] = {"packages": json.loads(metadata.stdout), "python": str(python)}
            declared = protocol["arms"][engine]
            if versions[engine]["packages"].get(declared["package"]) != declared["version"]:
                raise RuntimeError("installed engine differs from protocol version")
            if engine != "forge":
                lock = ROOT / "evals/comparison/environments" / engine / "uv.lock"
                versions[engine]["lock_sha256"] = digest(lock)
            for profile in ("minimal", "equivalent"):
                cases = protocol["scenarios"][:2] if profile == "minimal" else protocol["scenarios"]
                for declared in cases:
                    case = declared["id"]
                    root = run / engine / profile / case
                    root.mkdir(parents=True, mode=0o700)
                    runner = ActionRunner(python, engine, root)
                    try:
                        result = scenario(runner, case)
                    except (OSError, subprocess.SubprocessError) as exc:
                        result = {
                            "case": case,
                            "outcome": "not_measured",
                            "reason": type(exc).__name__,
                            "actions": runner.actions,
                        }
                    except Exception as exc:
                        result = {
                            "case": case,
                            "outcome": "failed",
                            "reason": type(exc).__name__,
                            "actions": runner.actions,
                        }
                    result.update(engine=engine, profile=profile)
                    results.append(result)
                    print(engine, profile, case, result["outcome"], flush=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            results.append(
                {
                    "engine": engine,
                    "profile": "setup",
                    "case": "environment",
                    "outcome": "not_measured",
                    "reason": type(exc).__name__,
                }
            )
    passed = sum(r["outcome"] == "passed" for r in results)
    report = {
        "schema_version": 1,
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": digest(PROTOCOL),
        "source_identity": start_identity,
        "source_unchanged": start_identity == snapshot(ROOT)["tree_digest"],
        "started_engines": engines,
        "versions": versions,
        "results": results,
        "passed": passed,
        "failed": sum(r["outcome"] == "failed" for r in results),
        "not_measured": sum(r["outcome"] == "not_measured" for r in results),
        "expected_results": len(engines) * (2 + len(protocol["scenarios"])),
        "business_qualified": False,
        "native_full_fault_comparison_complete": False,
        "independent_onboarding_measured": False,
        "core_advantage_proven": False,
        "decision": "insufficient_evidence_for_native_core_advantage",
        "attribution": {
            "minimal": "native agent loop + native approval/persistence, with shared business tool",
            "equivalent": (
                "shared host authorization, source invalidation, exact payload, "
                "request key and reconciliation around native engines"
            ),
        },
        "limits": protocol["first_iteration_limits"]
        + [
            "Source withdrawal covers host cached-result replay; native history was not measured",
            "Duplicates are sequential across processes; concurrent admission was not measured",
            "Host result reconciliation does not repair the native checkpoint",
            "Cancellation is injected at tool boundaries; distributed workers were not measured",
        ],
        "run_directory": str(run),
        "forgeharness_distribution_version": version("forgeharness"),
        "model_input_evidence_complete": all(
            "evidence" in result and complete_model_inputs(result["evidence"]) for result in results
        ),
    }
    report["integration_gate_passed"] = (
        report["source_unchanged"]
        and passed == report["expected_results"]
        and report["failed"] == report["not_measured"] == 0
        and report["model_input_evidence_complete"]
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (run / "summary.json").write_text(encoded)
    (output / "summary.json").write_text(encoded)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/framework-comparison")
    parser.add_argument(
        "--engines",
        nargs="+",
        choices=("forge", "deepagents", "pydantic"),
        default=["forge", "deepagents", "pydantic"],
    )
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    report = evaluate(args.output.resolve(), args.engines, prepare=args.prepare)
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("passed", "failed", "not_measured", "integration_gate_passed", "decision")
            }
        )
    )
    raise SystemExit(0 if report["integration_gate_passed"] else 1)


if __name__ == "__main__":
    main()

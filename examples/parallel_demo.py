"""Two overlapping specialists, lost response, fresh-process receipt recovery.

No model, network or credentials. All receipts/materials are synthetic and local.
Run ``uv run --no-sync python examples/parallel_demo.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from forgeharness.sdk import (
    Actor,
    AgentFinding,
    AgentReport,
    Collaboration,
    CollaborationAgent,
    CollaborationEvidence,
    CollaborationPlan,
    CollaborationTask,
    FinalAction,
    MessageRole,
    ModelDispatcher,
    ModelRequest,
    ModelResult,
    ModelUsage,
    ParallelExecution,
    ReconciledNode,
    ResourceScope,
    RunBudget,
    RunContext,
    Usage,
    verify_trace,
)


def assignment(request: ModelRequest) -> str:
    for message in reversed(request.messages):
        if message.role is MessageRole.USER:
            try:
                value = json.loads(message.content or "{}")
            except ValueError:
                continue
            if "collaboration_assignment" in value:
                return str(value["collaboration_assignment"])
    raise ValueError("missing registered assignment")


def report(task: str) -> ModelResult:
    key = "a" if task in {"alpha", "synthesis"} else "b"
    return ModelResult(
        action=FinalAction(
            content=AgentReport(
                task_id=task,
                summary=f"Synthetic {task} report",
                findings=(AgentFinding(claim="Synthetic evidence inspected", evidence_ids=(key,)),),
                unresolved=(),
            ).model_dump_json()
        ),
        usage=ModelUsage(input_tokens=20, output_tokens=10),
    )


def plan() -> CollaborationPlan:
    return CollaborationPlan(
        version="parallel-demo-v1",
        model_identity="scripted-parallel-demo-v1",
        goal="Inspect two independent synthetic contracts and synthesize the reports.",
        agents=tuple(
            CollaborationAgent(
                id=key,
                version="1",
                instructions="Inspect assigned evidence.",
                max_usage=Usage(steps=1, input_tokens=3000, output_tokens=500),
            )
            for key in ("alpha", "beta")
        ),
        evidence=tuple(
            CollaborationEvidence(
                id=key, resource_id=key, revision="1", text=f"Synthetic contract {key}."
            )
            for key in ("a", "b")
        ),
        tasks=(
            CollaborationTask(
                id="alpha", agent_id="alpha", objective="Inspect a", evidence_ids=("a",)
            ),
            CollaborationTask(
                id="beta", agent_id="beta", objective="Inspect b", evidence_ids=("b",)
            ),
        ),
        coordinator_id="alpha",
    )


def context() -> RunContext:
    return RunContext(
        application_id="collaboration",
        application_version="parallel-demo-v1",
        policy_version="collaboration-readonly-v1",
        run_id="parallel-demo",
        session_id="session",
        correlation_id="parallel-demo",
        actor=Actor(id="host"),
        scope=ResourceScope(tenant_id="synthetic", resources=("a", "b")),
    )


class ReceiptModel:
    def __init__(self, root: Path, *, recovery: bool):
        root.mkdir(parents=True, exist_ok=True)
        self.database = root / "receipts.sqlite"
        self.recovery = recovery
        self.ready = asyncio.Event()
        self.entered: set[str] = set()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS receipts(task TEXT PRIMARY KEY, output TEXT)"
            )

    async def decide(self, request: ModelRequest) -> ModelResult:
        task = assignment(request)
        if task != "synthesis":
            if self.recovery:
                with sqlite3.connect(self.database) as connection:
                    row = connection.execute(
                        "SELECT 1 FROM receipts WHERE task=?", (task,)
                    ).fetchone()
                if row is not None:
                    raise AssertionError("recovery must not repeat specialist inference")
            self.entered.add(task)
            if len(self.entered) == 2:
                self.ready.set()
            await self.ready.wait()  # neither returns before both enter provider
        result = report(task)
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO receipts VALUES(?,?)", (task, result.model_dump_json()))
        if task == "beta":
            raise TimeoutError("synthetic response lost after receipt committed")
        return result


async def execute(root: Path, action: str, node_id: str = "specialist.beta") -> dict:
    model = ReceiptModel(root, recovery=action != "run")

    async def reconcile(ctx, node, key):
        task = node.id.removeprefix("specialist.")
        with sqlite3.connect(model.database) as connection:
            row = connection.execute("SELECT output FROM receipts WHERE task=?", (task,)).fetchone()
        if row is None:
            return None
        result = ModelResult.model_validate_json(row[0])
        return ReconciledNode(
            output=result.model_dump(mode="json"),
            usage=Usage(
                steps=1,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            ),
            evidence_refs=(f"synthetic-receipt:{task}",),
        )

    with ModelDispatcher(
        state_dir=root / "capacity", provider_id="scripted-demo", capacity=2
    ) as dispatcher:
        group = Collaboration(
            plan=plan(),
            model_factory=lambda: model,
            budget=RunBudget(
                max_steps=3, max_tool_calls=0, max_input_tokens=9000, max_output_tokens=1500
            ),
            state_dir=root / "state",
            authorize_run=lambda ctx: True,
            validate_resources=lambda ctx, versions: versions == {"a": "1", "b": "1"},
            execution=ParallelExecution(max_in_flight=2, model_concurrency_safe=True),
            dispatcher=dispatcher,
            reconcile_node=reconcile,
        )
        if action == "run":
            result = await group.harness.run(context=context(), task=group.plan.goal)
        elif action == "reconcile":
            result = await group.harness.reconcile(context=context(), node_id=node_id)
        else:
            result = await group.harness.resume(context=context())
        snapshot = group.inspect(context())
        trace = group.harness.trace_path(context())
        return {
            "status": result.status.value,
            "usage": result.usage.model_dump(),
            "error": result.error,
            "trace_valid": verify_trace(trace).valid,
            "phases": [d.dispatch_phase for d in snapshot.delegations],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--action", choices=("run", "resume", "reconcile"))
    args = parser.parse_args()
    if args.root is not None and args.action is not None:
        print(json.dumps(asyncio.run(execute(args.root, args.action))))
        return
    with tempfile.TemporaryDirectory(prefix="forgeharness-parallel-") as temporary:
        root = Path(temporary)
        results = []
        for action in ("run", "reconcile", "resume"):
            child = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--root",
                    str(root),
                    "--action",
                    action,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )
            results.append(json.loads(child.stdout))
        assert [r["status"] for r in results] == ["running", "succeeded", "succeeded"]
        assert all(r["trace_valid"] for r in results)
        assert results[-1]["usage"] == {
            "steps": 3,
            "tool_calls": 0,
            "input_tokens": 60,
            "output_tokens": 30,
        }
        with sqlite3.connect(root / "receipts.sqlite") as connection:
            assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 3
        print(
            json.dumps(
                {
                    "parallel_barrier": True,
                    "fresh_process_recovery": True,
                    "no_duplicate_calls": True,
                    "results": results,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

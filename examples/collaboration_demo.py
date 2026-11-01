"""Three-process specialist recovery with scripted responses and synthetic receipts."""

from __future__ import annotations

import argparse
import asyncio
import json
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
    DurableNode,
    FinalAction,
    ModelRequest,
    ModelResult,
    ModelUsage,
    ReconciledNode,
    ResourceScope,
    RunBudget,
    RunContext,
    RunStatus,
    Usage,
)


def demo_plan() -> CollaborationPlan:
    allowance = Usage(steps=1, input_tokens=3_000, output_tokens=500)
    return CollaborationPlan(
        version="demo-v1",
        model_identity="offline-synthetic-receipt-model-v1",
        goal="Review the supplied synthetic implementation and test contracts.",
        agents=(
            CollaborationAgent(
                id="logic",
                version="1",
                instructions="Review implementation logic.",
                max_usage=allowance,
            ),
            CollaborationAgent(
                id="tests",
                version="1",
                instructions="Review test boundaries.",
                max_usage=allowance,
            ),
            CollaborationAgent(
                id="coordinator",
                version="1",
                instructions="Combine the two reviews.",
                max_usage=allowance,
            ),
        ),
        evidence=(
            CollaborationEvidence(
                id="implementation",
                resource_id="synthetic:implementation",
                revision="1",
                text="Read-only tasks must not dispatch a write tool.",
            ),
            CollaborationEvidence(
                id="test-contract",
                resource_id="synthetic:tests",
                revision="1",
                text="Test that a write tool request fails without executing the tool.",
            ),
        ),
        tasks=(
            CollaborationTask(
                id="logic",
                agent_id="logic",
                objective="Check the read-only constraint.",
                evidence_ids=("implementation",),
            ),
            CollaborationTask(
                id="tests",
                agent_id="tests",
                objective="Check the rejection regression.",
                evidence_ids=("test-contract",),
            ),
        ),
        coordinator_id="coordinator",
    )


def demo_report(task_id: str) -> AgentReport:
    keys = {
        "logic": ("implementation",),
        "tests": ("test-contract",),
        "synthesis": ("implementation", "test-contract"),
    }
    return AgentReport(
        task_id=task_id,
        summary="Synthetic contract review; no real model quality is measured.",
        findings=(
            AgentFinding(
                claim="The read-only contract requires a rejection test.",
                evidence_ids=keys[task_id],
            ),
        ),
        unresolved=(),
    )


def assignment(request: ModelRequest) -> str:
    for message in request.messages:
        if message.role.value != "user":
            continue
        try:
            value = json.loads(message.content or "")
        except ValueError:
            continue
        if isinstance(value, dict) and "collaboration_assignment" in value:
            return str(value["collaboration_assignment"])
    raise ValueError("missing registered assignment")


class DemoModel:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def decide(self, request: ModelRequest) -> ModelResult:
        assert request.tools == ()
        task_id = assignment(request)
        with (self.root / "calls.jsonl").open("a") as log:
            log.write(json.dumps({"task_id": task_id}) + "\n")
        result = ModelResult(
            action=FinalAction(content=demo_report(task_id).model_dump_json()),
            usage=ModelUsage(input_tokens=20, output_tokens=10),
            model_name="synthetic-no-inference",
        )
        receipt = ReconciledNode(
            output=result.model_dump(mode="json"),
            usage=Usage(steps=1, input_tokens=20, output_tokens=10),
            evidence_refs=("synthetic-receipt:" + task_id,),
        )
        (self.root / (task_id + ".receipt.json")).write_text(receipt.model_dump_json())
        if task_id == "tests":
            raise TimeoutError("synthetic response loss after receipt persistence")
        return result


def controller(root: Path) -> tuple[Collaboration, RunContext]:
    plan = demo_plan()

    async def reconcile(
        context: RunContext, node: DurableNode, stable_key: str
    ) -> ReconciledNode | None:
        del context, stable_key
        if node.id != "specialist.tests":
            return None
        path = root / "tests.receipt.json"
        return ReconciledNode.model_validate_json(path.read_text()) if path.exists() else None

    team = Collaboration(
        plan=plan,
        model_factory=lambda: DemoModel(root),
        budget=RunBudget(
            max_steps=3, max_tool_calls=0, max_input_tokens=9_000, max_output_tokens=1_500
        ),
        state_dir=root / "state",
        authorize_run=lambda ctx: (
            ctx.actor.id == "demo-host" and ctx.scope.tenant_id == "synthetic"
        ),
        validate_resources=lambda ctx, revisions: (
            revisions == {item.resource_id: item.revision for item in plan.evidence}
        ),
        reconcile_node=reconcile,
    )
    context = RunContext(
        application_id="collaboration",
        application_version=plan.version,
        policy_version="collaboration-readonly-v1",
        run_id="team-demo",
        session_id="demo",
        correlation_id="demo",
        actor=Actor(id="demo-host"),
        scope=ResourceScope(tenant_id="synthetic"),
    )
    return team, context


async def step(root: Path, stage: str) -> None:
    team, context = controller(root)
    if stage == "start":
        result = await team.harness.run(context=context, task=team.plan.goal)
        assert result.status is RunStatus.RUNNING
        assert result.error == "needs_reconciliation:specialist.tests"
    elif stage == "reconcile":
        result = await team.harness.reconcile(context=context, node_id="specialist.tests")
        assert result.status is RunStatus.SUCCEEDED
    else:
        before = (root / "calls.jsonl").read_text()
        result = await team.harness.resume(context=context)
        assert result.status is RunStatus.SUCCEEDED
        assert (root / "calls.jsonl").read_text() == before
        assert result.usage == Usage(steps=3, input_tokens=60, output_tokens=30)
    print(team.inspect(context).model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--stage", choices=("start", "reconcile", "resume"))
    args = parser.parse_args()
    if args.stage:
        if args.root is None:
            parser.error("--stage requires --root")
        asyncio.run(step(args.root, args.stage))
        return
    with tempfile.TemporaryDirectory(prefix="forge-team-demo-") as directory:
        for stage in ("start", "reconcile", "resume"):
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--root",
                    directory,
                    "--stage",
                    stage,
                ],
                check=True,
            )


if __name__ == "__main__":
    main()

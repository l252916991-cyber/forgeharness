"""Installed-wheel R2 smoke: three fresh processes, scoped temp files, no model."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from forgeharness.coding.application import coding_application
from forgeharness.sdk import (
    Actor,
    ApprovalGrant,
    DurableGraph,
    DurableNode,
    DurableOrchestrator,
    Harness,
    ResourceScope,
    RunContext,
    RunStatus,
    ScriptedModel,
    ToolCall,
    Usage,
)


def controller(root: Path) -> tuple[Harness, RunContext]:
    workspace = (root / "workspace").resolve()
    workspace.mkdir(exist_ok=True)
    (workspace / ".git").mkdir(exist_ok=True)
    graph = DurableGraph(
        version="demo-v1",
        model_identity="unused-scripted-model-v1",
        resource_versions={},
        nodes=(
            DurableNode(
                id="write",
                kind="tool",
                max_usage=Usage(tool_calls=1),
                call=ToolCall(
                    id="write",
                    name="write_file",
                    arguments={
                        "path": "r2.txt",
                        "content": "R2 scoped synthetic example\n",
                    },
                ),
            ),
            DurableNode(
                id="tests",
                kind="tool",
                dependencies=("write",),
                max_usage=Usage(tool_calls=1),
                call=ToolCall(id="tests", name="run_tests", arguments={}),
            ),
        ),
        final_node="tests",
    )
    ctx = RunContext(
        application_id="coding",
        application_version="1",
        policy_version="1",
        run_id="r2-demo",
        session_id="demo",
        correlation_id="demo",
        actor=Actor(id="host", roles=("developer", "maintainer")),
        scope=ResourceScope(tenant_id="local", workspace=workspace),
    )
    return Harness(
        coding_application(model_factory=lambda: ScriptedModel([]), test_command=("true",)),
        state_dir=root / "state",
        orchestrator=DurableOrchestrator(
            graph=graph,
            validate_resources=lambda context, revisions: context.scope.workspace == workspace,
        ),
    ), ctx


async def step(root: Path, stage: str) -> None:
    harness, context = controller(root)
    if stage == "start":
        result = await harness.run(context=context, task="write and verify synthetic sample")
        assert result.status is RunStatus.AWAITING_APPROVAL
        assert result.state_version == 2 and result.recovery_semantics_version == 5
    elif stage == "approve":
        grant = harness.approve(context=context, approver=context, node_id="write")
        write_grant(root, grant)
    else:
        result = await harness.resume(context=context, grant=read_grant(root), approver=context)
        assert result.status is RunStatus.SUCCEEDED, result
        assert result.usage.tool_calls == 2
        assert all(n.status == "completed" for n in harness.inspect_orchestration(context).nodes)
        repeated = await harness.resume(context=context)
        assert repeated == result
        print("R2 three-process approval, write, tests and terminal reuse passed")


def write_grant(root: Path, grant: ApprovalGrant) -> None:
    (root / "grant.json").write_text(grant.model_dump_json())


def read_grant(root: Path) -> ApprovalGrant:
    return ApprovalGrant.model_validate_json((root / "grant.json").read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--stage", choices=("start", "approve", "resume"))
    args = parser.parse_args()
    if args.stage is not None:
        if args.root is None:
            parser.error("--stage requires --root")
        asyncio.run(step(args.root, args.stage))
        return
    with tempfile.TemporaryDirectory(prefix="forge-r2-demo-") as directory:
        for stage in ("start", "approve", "resume"):
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

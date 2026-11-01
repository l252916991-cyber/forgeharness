"""Synthetic R2 subprocess fixture; never opens host business data or a network."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    AuthorizationRequest,
    BusinessReceipt,
    DurableGraph,
    DurableNode,
    DurableOrchestrator,
    FinalAction,
    Harness,
    ModelRequest,
    ModelResult,
    ModelUsage,
    NoopVerifier,
    PolicyDecision,
    PolicyDecisionType,
    ReconciledNode,
    ResourceScope,
    RiskLevel,
    RunBudget,
    RunContext,
    ToolCall,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    Usage,
)
from forgeharness.sdk.durable import DurableRunner
from forgeharness.state.approval import ApprovalGrant, SQLiteApprovalLedger
from forgeharness.state.invocation_journal import SQLiteInvocationJournal
from forgeharness.state.orchestration import OrchestrationStore


def crash(point: str) -> None:
    if os.environ.get("R2_CRASH_AT") == point:
        os._exit(91)


class Inputs(BaseModel):
    value: str = "synthetic"


class ReceiptTool:
    input_model = Inputs

    def __init__(self, root: Path, *, idempotent: bool = False) -> None:
        self.root = root
        self.spec = ToolSpec(
            name="write",
            description="synthetic receipt store",
            input_schema=Inputs.model_json_schema(),
            risk=RiskLevel.WRITE,
            effect_class=(
                ToolEffectClass.IDEMPOTENT if idempotent else ToolEffectClass.NON_IDEMPOTENT
            ),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        with sqlite3.connect(self.root / "effects.sqlite") as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS effects (key TEXT, value TEXT)")
            row = connection.execute(
                "SELECT value FROM effects WHERE key=?", (context.idempotency_key,)
            ).fetchone()
            if row is None or self.spec.effect_class is ToolEffectClass.NON_IDEMPOTENT:
                connection.execute(
                    "INSERT INTO effects VALUES (?,?)",
                    (context.idempotency_key, arguments.model_dump()["value"]),
                )
        crash("effect-committed")
        return ToolOutput(
            ok=True,
            content="synthetic receipt",
            receipt=BusinessReceipt(resource_id="synthetic", status="completed", version="1"),
        )


class FixtureModel:
    async def decide(self, request: ModelRequest) -> ModelResult:
        del request
        if os.environ.get("R2_MODEL_TIMEOUT") == "1":
            raise TimeoutError("synthetic provider timeout after dispatch")
        crash("model-response")
        return ModelResult(
            action=FinalAction(content="synthetic model response"),
            usage=ModelUsage(input_tokens=7, output_tokens=3),
        )


class FixturePolicy:
    def __init__(self, *, approval: bool = False) -> None:
        self.approval = approval

    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        del request
        return PolicyDecision(
            type=PolicyDecisionType.REQUIRE_APPROVAL if self.approval else PolicyDecisionType.ALLOW,
            reason="fixture policy",
        )

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool:
        del owner, call
        return requester.actor.id == "approver"


def context(*, approver: bool = False) -> RunContext:
    return RunContext(
        application_id="r2-fixture",
        application_version="1",
        policy_version="1",
        run_id="run-1",
        session_id="session-1",
        actor=Actor(id="approver" if approver else "owner"),
        scope=ResourceScope(tenant_id="test", resources=("synthetic",)),
        correlation_id="test",
    )


def graph(*, model: bool = False, parallel: bool = False) -> DurableGraph:
    nodes = [
        DurableNode(
            id="model" if model else "write",
            kind="model" if model else "tool",
            call=None if model else ToolCall(id="ignored", name="write", arguments={}),
            max_usage=Usage(steps=1, input_tokens=1000, output_tokens=100)
            if model
            else Usage(tool_calls=1),
        )
    ]
    if parallel:
        nodes.append(nodes[0].model_copy(update={"id": "branch-2"}))
        nodes.append(DurableNode(id="join", kind="join", dependencies=(nodes[0].id, "branch-2")))
    return DurableGraph(
        version="1",
        model_identity="fixture-model-v1",
        resource_versions={"synthetic": "1"},
        nodes=tuple(nodes),
        final_node=nodes[-1].id,
    )


def harness(
    root: Path,
    *,
    approval: bool = False,
    model: bool = False,
    parallel: bool = False,
    idempotent: bool = False,
    budget: RunBudget | None = None,
) -> Harness:
    async def reconcile(ctx: RunContext, node: DurableNode, key: str) -> ReconciledNode | None:
        del ctx
        if node.kind == "model":
            return None
        if not (root / "effects.sqlite").is_file():
            return None
        with sqlite3.connect(root / "effects.sqlite") as connection:
            row = connection.execute("SELECT value FROM effects WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return ReconciledNode(
            output=ToolOutput(
                ok=True,
                content="synthetic receipt",
                receipt=BusinessReceipt(resource_id="synthetic", status="completed", version="1"),
            ).model_dump(mode="json"),
            usage=Usage(tool_calls=1),
            evidence_refs=("fixture:existing-receipt",),
        )

    return Harness(
        ApplicationSpec(
            id="r2-fixture",
            version="1",
            policy_version="1",
            model_factory=FixtureModel,
            tools=(ReceiptTool(root, idempotent=idempotent),),
            policy=FixturePolicy(approval=approval),
            verifier=NoopVerifier(),
            budget=budget or RunBudget(),
        ),
        state_dir=root / "state",
        orchestrator=DurableOrchestrator(
            graph=graph(model=model, parallel=parallel),
            validate_resources=lambda ctx, versions: not (root / "revoked").exists(),
            reconcile_node=reconcile,
        ),
    )


def barriers() -> None:
    save = OrchestrationStore.save
    settle = SQLiteInvocationJournal.settle
    started = SQLiteInvocationJournal.mark_started
    consume = SQLiteApprovalLedger._consume
    acknowledge = OrchestrationStore.acknowledge
    advance = DurableRunner.advance

    def saved(self: Any, state: Any, **kwargs: Any) -> Any:
        result = save(self, state, **kwargs)
        crash(kwargs["event"])
        return result

    def settled(self: Any, record: Any) -> Any:
        result = settle(self, record)
        crash("journal-settled")
        return result

    def journal_started(self: Any, invocation_id: str, **kwargs: Any) -> Any:
        result = started(self, invocation_id, **kwargs)
        crash("journal-started")
        return result

    def consumed(self: Any, *args: Any) -> Any:
        crash("consume-before")
        result = consume(self, *args)
        crash("consume-after")
        return result

    def acknowledged(self: Any, *args: Any) -> None:
        crash("event-published")
        acknowledge(self, *args)

    def wait_for_release() -> None:
        print("R2_OWNER_READY", flush=True)
        sys.stdin.readline()

    async def held_advance(self: Any) -> Any:
        if os.environ.get("R2_HOLD_OWNER") == "1":
            await asyncio.to_thread(wait_for_release)
        return await advance(self)

    OrchestrationStore.save = saved
    SQLiteInvocationJournal.settle = settled
    SQLiteInvocationJournal.mark_started = journal_started
    SQLiteApprovalLedger._consume = consumed
    OrchestrationStore.acknowledge = acknowledged
    DurableRunner.advance = held_advance


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("action", choices=("run", "resume", "approve", "reconcile"))
    parser.add_argument("--approval", action="store_true")
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--parallel", action="store_true")
    parser.add_argument("--idempotent", action="store_true")
    args = parser.parse_args()
    barriers()
    controller = harness(
        args.root,
        approval=args.approval,
        model=args.model,
        parallel=args.parallel,
        idempotent=args.idempotent,
    )
    if args.action == "approve":
        grant = controller.approve(
            context=context(), approver=context(approver=True), ttl=timedelta(hours=1)
        )
        (args.root / "grant.json").write_text(grant.model_dump_json())
        return
    if args.action == "run":
        result = await controller.run(context=context(), task="synthetic task")
    elif args.action == "reconcile":
        result = await controller.reconcile(context=context(), node_id="write")
    else:
        grant_path = args.root / "grant.json"
        grant = (
            ApprovalGrant.model_validate_json(grant_path.read_text())
            if grant_path.exists()
            else None
        )
        result = await controller.resume(
            context=context(), grant=grant, approver=context(approver=True) if grant else None
        )
    print(json.dumps(result.model_dump(mode="json")))


if __name__ == "__main__":
    asyncio.run(main())

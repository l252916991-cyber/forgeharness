"""R2 fault matrix: process death, atomic approvals, unknown effects and joins."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from forgeharness.models.base import ModelProtocolError
from forgeharness.sdk import (
    ApprovalError,
    CheckpointConflict,
    DurableGraph,
    DurableNode,
    FinalAction,
    Harness,
    ModelResult,
    ModelUsage,
    ReconciledNode,
    RunBudget,
    RunStatus,
    ScopeError,
    ToolCall,
    ToolOutput,
    Usage,
)
from forgeharness.sdk.durable import DurableRunner
from forgeharness.state.orchestration import OrchestrationState, OrchestrationStore
from forgeharness.state.sqlite import connect_wal
from forgeharness.tools.base import ToolErrorCode, ToolFailure

FIXTURE = Path(__file__).parents[1] / "integration/r2_fixture.py"
spec = importlib.util.spec_from_file_location("r2_fixture", FIXTURE)
assert spec is not None and spec.loader is not None
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def worker(
    root: Path, action: str, *, crash: str = "", options: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, R2_CRASH_AT=crash)
    return subprocess.run(
        [sys.executable, str(FIXTURE), str(root), action, *options],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def outcome(process: subprocess.CompletedProcess[str]) -> dict:
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def effects(root: Path) -> int:
    if not (root / "effects.sqlite").exists():
        return 0
    with sqlite3.connect(root / "effects.sqlite") as connection:
        return int(connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0])


@pytest.mark.parametrize(
    "point", ["created", "admitted:write", "journal-settled", "settled:write", "finished"]
)
def test_new_process_reuses_admission_journal_and_terminal(tmp_path: Path, point: str) -> None:
    first = worker(tmp_path, "run", crash=point)
    assert first.returncode == 91, first.stderr
    result = outcome(worker(tmp_path, "resume"))
    assert result["status"] == "succeeded"
    assert result["usage"]["tool_calls"] == 1
    assert effects(tmp_path) == 1
    controller = fixture.harness(tmp_path)
    snapshot = controller.inspect_orchestration(fixture.context())
    assert all(n.status == "completed" and n.reservation == Usage() for n in snapshot.nodes)
    assert (
        outcome(worker(tmp_path, "resume"))["checkpoint_revision"] == result["checkpoint_revision"]
    )
    assert OrchestrationStore(controller.database).events(result["task_id"]) == ()


@pytest.mark.parametrize("idempotent", [False, True])
@pytest.mark.parametrize("point", ["journal-started", "effect-committed"])
def test_unknown_effect_never_replays_even_when_idempotent(
    tmp_path: Path, point: str, idempotent: bool
) -> None:
    options = ("--idempotent",) if idempotent else ()
    first = worker(tmp_path, "run", crash=point, options=options)
    assert first.returncode == 91, first.stderr
    before = effects(tmp_path)
    for _ in range(2):
        result = outcome(worker(tmp_path, "resume", options=options))
        assert result["status"] == "running"
        assert result["error"] == "needs_reconciliation:write"
        assert effects(tmp_path) == before
    snapshot = fixture.harness(tmp_path, idempotent=idempotent).inspect_orchestration(
        fixture.context()
    )
    assert snapshot.nodes[0].reservation == Usage(tool_calls=1)
    if point == "effect-committed":
        resolved = outcome(worker(tmp_path, "reconcile", options=options))
        assert resolved["status"] == "succeeded"
        assert resolved["usage"]["tool_calls"] == 1
        assert effects(tmp_path) == 1


@pytest.mark.parametrize(
    "point", ["consume-before", "consume-after", "admitted:write", "journal-settled"]
)
def test_approval_consume_and_admit_crash_windows(tmp_path: Path, point: str) -> None:
    options = ("--approval",)
    assert outcome(worker(tmp_path, "run", options=options))["status"] == "awaiting_approval"
    assert worker(tmp_path, "approve", options=options).returncode == 0
    first = worker(tmp_path, "resume", crash=point, options=options)
    assert first.returncode == 91, first.stderr
    with sqlite3.connect(tmp_path / "state/runs.sqlite") as connection:
        consumed = connection.execute("SELECT consumed FROM approval_grants").fetchone()[0]
    assert consumed == int(point in {"admitted:write", "journal-settled"})
    if consumed:
        (tmp_path / "grant.json").unlink()
    result = outcome(worker(tmp_path, "resume", options=options))
    assert result["status"] == "succeeded"
    assert effects(tmp_path) == 1
    assert result["usage"]["tool_calls"] == 1


def test_model_response_loss_holds_reservation_and_never_resends(tmp_path: Path) -> None:
    assert worker(tmp_path, "run", crash="model-response", options=("--model",)).returncode == 91
    for _ in range(2):
        result = outcome(worker(tmp_path, "resume", options=("--model",)))
        assert result["status"] == "running"
        assert result["error"] == "needs_reconciliation:model"
    snapshot = fixture.harness(tmp_path, model=True).inspect_orchestration(fixture.context())
    assert snapshot.nodes[0].status == "unknown"
    assert snapshot.nodes[0].reservation == Usage(steps=1, input_tokens=1000, output_tokens=100)


def test_model_timeout_is_not_a_free_retry_after_process_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("R2_MODEL_TIMEOUT", "1")
    first = outcome(worker(tmp_path, "run", options=("--model",)))
    assert first["error"] == "needs_reconciliation:model"
    monkeypatch.delenv("R2_MODEL_TIMEOUT")
    for _ in range(2):
        result = outcome(worker(tmp_path, "resume", options=("--model",)))
        assert result["error"] == "needs_reconciliation:model"
        assert result["status"] == "running"
    state = fixture.harness(tmp_path, model=True).inspect_orchestration(fixture.context())
    assert state.nodes[0].status == "unknown"
    assert state.nodes[0].reservation == Usage(steps=1, input_tokens=1000, output_tokens=100)


async def test_parallel_approval_collection_and_join_are_durable(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, approval=True, parallel=True)
    result = await controller.run(context=fixture.context(), task="synthetic task")
    assert result.status is RunStatus.AWAITING_APPROVAL
    snapshot = controller.inspect_orchestration(fixture.context())
    assert [n.id for n in snapshot.nodes if n.status == "awaiting_approval"] == [
        "write",
        "branch-2",
    ]
    with pytest.raises(ScopeError, match="node_id"):
        controller.approve(context=fixture.context(), approver=fixture.context(approver=True))
    second = controller.approve(
        context=fixture.context(), approver=fixture.context(approver=True), node_id="branch-2"
    )
    first = controller.approve(
        context=fixture.context(), approver=fixture.context(approver=True), node_id="write"
    )
    fresh = fixture.harness(tmp_path, approval=True, parallel=True)
    result = await fresh.resume(
        context=fixture.context(), grant=second, approver=fixture.context(approver=True)
    )
    assert result.status is RunStatus.AWAITING_APPROVAL
    assert effects(tmp_path) == 1
    assert fresh.inspect_orchestration(fixture.context()).nodes[2].status == "pending"
    with pytest.raises(ApprovalError):
        await fresh.resume(
            context=fixture.context(), grant=second, approver=fixture.context(approver=True)
        )
    result = await fixture.harness(tmp_path, approval=True, parallel=True).resume(
        context=fixture.context(), grant=first, approver=fixture.context(approver=True)
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage.tool_calls == 2
    assert effects(tmp_path) == 2
    assert (
        fixture.harness(tmp_path, approval=True, parallel=True)
        .inspect_orchestration(fixture.context())
        .nodes[2]
        .status
        == "completed"
    )


async def test_reservation_competition_exact_limit_and_overrun(tmp_path: Path) -> None:
    controller = fixture.harness(
        tmp_path,
        parallel=True,
        budget=RunBudget(max_tool_calls=1, max_input_tokens=0, max_output_tokens=0),
    )
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.EXHAUSTED
    assert result.usage.tool_calls == 1
    assert effects(tmp_path) == 1
    # Known nested usage is charged even when a tool violates its allowance.
    other = tmp_path / "nested"
    other.mkdir()
    controller = fixture.harness(other)
    tool = controller.application.tools[0]

    async def nested(arguments, context):
        return ToolOutput(ok=True, content="overrun", usage=Usage(input_tokens=1, tool_calls=1))

    tool.execute = nested
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.FAILED
    assert result.usage == Usage(tool_calls=2, input_tokens=1)


async def test_failed_branch_keeps_independent_result_and_blocks_join(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, parallel=True)

    async def partial(arguments, context):
        if context.logical_call_id == "write":
            return ToolOutput(
                ok=False,
                content="invalid",
                usage=Usage(),
                error=ToolFailure(
                    code=ToolErrorCode.INVALID_ARGUMENTS, message="invalid", recoverable=False
                ),
            )
        return ToolOutput(ok=True, content="independent result")

    controller.application.tools[0].execute = partial
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.FAILED
    assert result.usage.tool_calls == 2
    state = controller.inspect_orchestration(fixture.context())
    assert [n.status for n in state.nodes] == ["failed", "completed", "pending"]


async def test_cancel_request_stops_future_admission_but_retains_receipt(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, parallel=True)
    tool = controller.application.tools[0]
    execute = tool.execute
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(arguments, context):
        entered.set()
        await release.wait()
        return await execute(arguments, context)

    tool.execute = blocked
    task = asyncio.create_task(controller.run(context=fixture.context(), task="test"))
    await entered.wait()
    other = fixture.harness(tmp_path, parallel=True)
    assert other.cancel(fixture.context()) is True
    with pytest.raises(ScopeError, match="active worker"):
        await other.resume(context=fixture.context())
    release.set()
    result = await task
    assert result.status is RunStatus.CANCELLED
    assert result.usage.tool_calls == 1 and effects(tmp_path) == 1
    snapshot = other.inspect_orchestration(fixture.context())
    assert [n.status for n in snapshot.nodes] == ["completed", "cancelled", "cancelled"]
    assert other.cancel(fixture.context()) is False


async def test_access_definition_and_future_state_fail_closed(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path)
    await controller.run(context=fixture.context(), task="test")
    with pytest.raises(CheckpointConflict):
        await controller.run(context=fixture.context(), task="other")
    (tmp_path / "revoked").touch()
    with pytest.raises(ScopeError, match="resources"):
        controller.inspect(fixture.context())
    (tmp_path / "revoked").unlink()
    changed = replace(
        controller.orchestrator,
        graph=controller.orchestrator.graph.model_copy(update={"version": "2"}),
    )
    with pytest.raises(ScopeError, match="identity"):
        Harness(controller.application, state_dir=tmp_path / "state", orchestrator=changed).inspect(
            fixture.context()
        )
    # An old ReAct or one-shot registration cannot serve/recover an R2 result.
    old = Harness(controller.application, state_dir=tmp_path / "state")
    with pytest.raises(ScopeError, match="durable"):
        old.inspect(fixture.context())
    with connect_wal(controller.database) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM orchestration_runs").fetchone()[0]
        )
        payload["schema_version"] = 999
        connection.execute("UPDATE orchestration_runs SET payload=?", (json.dumps(payload),))
    with pytest.raises(ValidationError):
        controller.inspect(fixture.context())


def test_graph_rejects_unrebuildable_shapes() -> None:
    join = DurableNode(id="join", kind="join", dependencies=("join",))
    with pytest.raises(ValidationError, match="cyclic"):
        DurableGraph(
            version="1",
            model_identity="test",
            resource_versions={},
            nodes=(join,),
            final_node="join",
        )
    with pytest.raises(ValidationError, match="reservation"):
        DurableNode(id="write", kind="tool", call=ToolCall(id="x", name="write", arguments={}))


async def test_deadline_survives_restart_and_clock_reversal_is_refused(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, approval=True, budget=RunBudget(max_wall_seconds=1))
    await controller.run(context=fixture.context(), task="test")
    with connect_wal(controller.database) as connection:
        raw = connection.execute("SELECT payload FROM orchestration_runs").fetchone()[0]
        state = OrchestrationState.model_validate_json(raw)
        expired = state.model_copy(update={"deadline": datetime.now(UTC) - timedelta(seconds=1)})
        connection.execute("UPDATE orchestration_runs SET payload=?", (expired.model_dump_json(),))
    result = await controller.resume(context=fixture.context())
    assert result.status is RunStatus.EXHAUSTED
    assert effects(tmp_path) == 0


async def test_delete_erases_r2_state_outbox_and_prevents_reuse(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path)
    await controller.run(context=fixture.context(), task="test")
    controller.delete(fixture.context())
    with connect_wal(controller.database) as connection:
        for table in (
            "orchestration_runs",
            "orchestration_outbox",
            "approval_grants",
            "tool_invocations",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(ScopeError, match="deleted"):
        await controller.run(context=fixture.context(), task="test")


async def test_model_result_and_unknown_response_reconciliation(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, model=True)
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage == Usage(steps=1, input_tokens=7, output_tokens=3)
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    assert worker(unknown, "run", crash="model-response", options=("--model",)).returncode == 91
    controller = fixture.harness(unknown, model=True)
    await controller.resume(context=fixture.context())

    async def resolver(context, node, key):
        return ReconciledNode(
            output=ModelResult(
                action=FinalAction(content="recovered"),
                usage=ModelUsage(input_tokens=7, output_tokens=3),
            ).model_dump(mode="json"),
            usage=Usage(steps=1, input_tokens=7, output_tokens=3),
            evidence_refs=("provider:stored-response",),
        )

    controller.orchestrator = replace(controller.orchestrator, reconcile_node=resolver)
    result = await controller.reconcile(context=fixture.context(), node_id="model")
    assert result.status is RunStatus.SUCCEEDED and result.final_output == "recovered"
    state = controller.inspect_orchestration(fixture.context())
    assert state.nodes[0].evidence_refs == ("provider:stored-response",)
    with pytest.raises(ScopeError, match="unknown"):
        await controller.reconcile(context=fixture.context(), node_id="model")


@pytest.mark.parametrize("reported", [True, False])
async def test_model_protocol_failure_does_not_invent_free_usage(
    tmp_path: Path, reported: bool
) -> None:
    class ViolatingModel:
        async def decide(self, request):
            raise ModelProtocolError(
                "fixture",
                code="multiple_tool_calls_not_allowed",
                usage=ModelUsage(input_tokens=8, output_tokens=4) if reported else None,
            )

    controller = fixture.harness(tmp_path, model=True)
    controller.application = replace(controller.application, model_factory=ViolatingModel)
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is (RunStatus.FAILED if reported else RunStatus.RUNNING)
    assert result.usage.steps == 1
    if reported:
        assert result.usage.input_tokens == 8 and result.usage.output_tokens == 4
    else:
        assert "needs_reconciliation" in result.error
        assert (
            controller.inspect_orchestration(fixture.context()).nodes[0].reservation.input_tokens
            == 1000
        )


async def test_preparation_rejection_has_no_provider_cost(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, model=True)
    node = controller.orchestrator.graph.nodes[0].model_copy(
        update={"max_usage": Usage(steps=1, input_tokens=1)}
    )
    controller.orchestrator = replace(
        controller.orchestrator,
        graph=controller.orchestrator.graph.model_copy(update={"nodes": (node,)}),
    )
    result = await controller.run(context=fixture.context(), task="long fixture message" * 10)
    assert result.status is RunStatus.FAILED
    assert result.usage == Usage()
    assert controller.inspect_orchestration(fixture.context()).nodes[0].reservation == Usage()


async def test_retry_attempts_and_nested_usage_are_charged_once(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, idempotent=True, budget=RunBudget(initial_backoff_ms=0))
    allowances = []

    async def scripted(arguments, context):
        allowances.append(context.allowance)
        if len(allowances) == 1:
            return ToolOutput(
                ok=False,
                content="temporary",
                usage=Usage(input_tokens=2),
                error=ToolFailure(
                    code=ToolErrorCode.TIMEOUT, message="temporary", recoverable=True
                ),
            )
        return ToolOutput(ok=True, content="done", usage=Usage(input_tokens=3))

    controller.application.tools[0].execute = scripted
    node = controller.orchestrator.graph.nodes[0].model_copy(
        update={"max_usage": Usage(tool_calls=2, input_tokens=5)}
    )
    controller.orchestrator = replace(
        controller.orchestrator,
        graph=controller.orchestrator.graph.model_copy(update={"nodes": (node,)}),
    )
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage == Usage(tool_calls=2, input_tokens=5)
    assert len(allowances) == 2
    assert allowances[1].remaining_tool_calls == 0
    assert allowances[1].remaining_input_tokens == 3
    assert (await controller.resume(context=fixture.context())).usage == result.usage


async def test_cancel_does_not_hide_unknown_effect(tmp_path: Path) -> None:
    assert worker(tmp_path, "run", crash="effect-committed").returncode == 91
    controller = fixture.harness(tmp_path)
    assert controller.cancel(fixture.context()) is True
    result = await controller.resume(context=fixture.context())
    assert result.status is RunStatus.RUNNING and "needs_reconciliation" in result.error
    assert controller.inspect_orchestration(fixture.context()).nodes[0].reservation.tool_calls == 1
    result = await controller.reconcile(context=fixture.context(), node_id="write")
    assert result.status is RunStatus.CANCELLED
    assert effects(tmp_path) == 1 and result.usage.tool_calls == 1


def test_outbox_republication_has_stable_id_and_no_uncommitted_event(tmp_path: Path) -> None:
    assert worker(tmp_path, "run", crash="event-published").returncode == 91
    result = outcome(worker(tmp_path, "resume"))
    path = fixture.harness(tmp_path).trace_path(fixture.context())
    # Hash-chain records wrap the public trace event under `event`.
    records = [json.loads(line) for line in path.read_text().splitlines()]
    events = [record.get("event", record) for record in records]
    transitions = [event for event in events if event.get("type") == "orchestration.transition"]
    ids = [event["payload"]["event_id"] for event in transitions]
    assert len(ids) > len(set(ids))
    assert len(set(ids)) == result["checkpoint_revision"]
    assert all(
        event["payload"]["revision"] <= result["checkpoint_revision"] for event in transitions
    )


async def test_owner_fencing_and_terminal_immutability(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, approval=True)
    await controller.run(context=fixture.context(), task="test")
    state = controller.inspect_orchestration(fixture.context())
    store = OrchestrationStore(controller.database)
    current = store.take_ownership(state, "new-owner")
    with pytest.raises(CheckpointConflict, match="owner"):
        store.save(state, event="stale")
    # Fencing must also stop an old worker before its next physical attempt.
    runner = DurableRunner(controller, fixture.context())
    runner.state = state
    with pytest.raises(CheckpointConflict, match="owner"):
        runner._guard()
    assert current.owner == store.load(state.result.task_id).owner
    grant = controller.approve(context=fixture.context(), approver=fixture.context(approver=True))
    await controller.resume(
        context=fixture.context(), grant=grant, approver=fixture.context(approver=True)
    )
    terminal = controller.inspect_orchestration(fixture.context())
    with pytest.raises(CheckpointConflict, match="terminal"):
        store.save(
            terminal.model_copy(
                update={"result": terminal.result.model_copy(update={"status": RunStatus.RUNNING})}
            ),
            event="regression",
        )


async def test_expired_and_revoked_approver_cannot_admit(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, approval=True)
    await controller.run(context=fixture.context(), task="test")
    grant = controller.approve(context=fixture.context(), approver=fixture.context(approver=True))
    expired = grant.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(ApprovalError, match="expired"):
        await controller.resume(
            context=fixture.context(), grant=expired, approver=fixture.context(approver=True)
        )
    controller.application = replace(
        controller.application, authorize_run=lambda ctx: ctx.actor.id == "owner"
    )
    with pytest.raises(ScopeError, match="authorized"):
        await controller.resume(
            context=fixture.context(), grant=grant, approver=fixture.context(approver=True)
        )
    assert effects(tmp_path) == 0
    with sqlite3.connect(controller.database) as connection:
        assert connection.execute("SELECT consumed FROM approval_grants").fetchone()[0] == 0


async def test_clock_backwards_and_resource_change_while_verifying_fail_closed(
    tmp_path: Path,
) -> None:
    controller = fixture.harness(tmp_path, approval=True)
    await controller.run(context=fixture.context(), task="test")
    with connect_wal(controller.database) as connection:
        raw = connection.execute("SELECT payload FROM orchestration_runs").fetchone()[0]
        state = OrchestrationState.model_validate_json(raw)
        future = state.model_copy(update={"observed_at": datetime.now(UTC) + timedelta(days=1)})
        connection.execute("UPDATE orchestration_runs SET payload=?", (future.model_dump_json(),))
    with pytest.raises(ScopeError, match="backwards"):
        await controller.resume(context=fixture.context())
    other = tmp_path / "verify"
    other.mkdir()
    controller = fixture.harness(other)

    class RevokingVerifier:
        async def verify(self, request):
            (other / "revoked").touch()
            from forgeharness.sdk import VerificationResult

            return VerificationResult(passed=True, reason="test")

    controller.application = replace(controller.application, verifier=RevokingVerifier())
    with pytest.raises(ScopeError, match="resources"):
        await controller.run(context=fixture.context(), task="test")
    assert effects(other) == 1
    with pytest.raises(ScopeError):
        controller.inspect(fixture.context())


def test_two_process_resumers_cannot_both_advance(tmp_path: Path) -> None:
    assert worker(tmp_path, "run", crash="created").returncode == 91
    first = subprocess.Popen(
        [sys.executable, str(FIXTURE), str(tmp_path), "resume"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, R2_HOLD_OWNER="1"),
    )
    try:
        assert first.stdout.readline().strip() == "R2_OWNER_READY"
        second = worker(tmp_path, "resume")
        assert second.returncode != 0 and "active worker" in second.stderr
        stdout, stderr = first.communicate("release\n", timeout=10)
        assert first.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "succeeded"
        assert effects(tmp_path) == 1
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)


@pytest.mark.parametrize("tokens", [1000, 1001])
async def test_model_equal_limit_completes_and_over_limit_retains_cost(
    tmp_path: Path, tokens: int
) -> None:
    class MeteredModel:
        async def decide(self, request):
            return ModelResult(
                action=FinalAction(content="result"),
                usage=ModelUsage(input_tokens=tokens, output_tokens=100),
            )

    controller = fixture.harness(
        tmp_path, model=True, budget=RunBudget(max_input_tokens=1000, max_output_tokens=100)
    )
    controller.application = replace(controller.application, model_factory=MeteredModel)
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is (RunStatus.SUCCEEDED if tokens == 1000 else RunStatus.FAILED)
    assert result.usage.input_tokens == tokens and result.usage.output_tokens == 100


async def test_unknown_usage_is_not_released_to_an_independent_branch(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, parallel=True, budget=RunBudget(max_tool_calls=1))

    async def failed(arguments, context):
        raise RuntimeError("unknown nested usage")

    controller.application.tools[0].execute = failed
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.RUNNING and "needs_reconciliation" in result.error
    state = controller.inspect_orchestration(fixture.context())
    assert state.nodes[0].status == "unknown"
    assert state.nodes[0].reservation.tool_calls == 1
    assert state.nodes[1].status == "pending"
    assert result.usage.tool_calls == 1


async def test_known_overrun_with_unknown_effect_blocks_further_spending(tmp_path: Path) -> None:
    controller = fixture.harness(tmp_path, parallel=True, budget=RunBudget(max_input_tokens=5))
    graph = controller.orchestrator.graph
    first = graph.nodes[0].model_copy(update={"max_usage": Usage(tool_calls=1, input_tokens=5)})
    controller.orchestrator = replace(
        controller.orchestrator, graph=graph.model_copy(update={"nodes": (first, *graph.nodes[1:])})
    )
    calls = []

    async def overrun(arguments, context):
        calls.append(context.logical_call_id)
        return ToolOutput(
            ok=False,
            content="unknown effect",
            usage=Usage(input_tokens=6),
            error=ToolFailure(code=ToolErrorCode.TIMEOUT, message="timeout", recoverable=True),
        )

    controller.application.tools[0].execute = overrun
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.RUNNING and "needs_reconciliation" in result.error
    assert result.usage.input_tokens == 6
    assert calls == ["write"]
    assert controller.inspect_orchestration(fixture.context()).nodes[1].status == "pending"


async def test_direct_react_runtime_refuses_r2_and_future_recovery(tmp_path: Path) -> None:
    assert worker(tmp_path, "run", crash="effect-committed").returncode == 91
    controller = fixture.harness(tmp_path)
    previous = controller.inspect(fixture.context())
    runtime, _ = controller._runtime(fixture.context())
    for wrong in (
        previous,
        previous.model_copy(update={"state_version": 1, "recovery_semantics_version": 999}),
    ):
        with pytest.raises(ValueError, match="incompatible"):
            await runtime.resume_running(previous=wrong, workspace=None)
    assert controller.inspect(fixture.context()).checkpoint_revision == previous.checkpoint_revision
    assert effects(tmp_path) == 1


@pytest.mark.parametrize("change", ["arguments", "resources", "budget"])
async def test_active_run_cannot_change_its_frozen_request(tmp_path: Path, change: str) -> None:
    controller = fixture.harness(tmp_path, parallel=True)
    graph = controller.orchestrator.graph
    original_app = controller.application
    tool = original_app.tools[0]
    execute = tool.execute

    async def mutate(arguments, context):
        output = await execute(arguments, context)
        if context.logical_call_id == "write":
            if change == "arguments":
                graph.nodes[1].call.arguments["value"] = "changed-after-start"
            elif change == "resources":
                graph.resource_versions["synthetic"] = "2"
            else:
                controller.application = replace(original_app, budget=RunBudget(max_tool_calls=100))
        return output

    tool.execute = mutate
    with pytest.raises(ScopeError, match="identity changed during execution"):
        await controller.run(context=fixture.context(), task="immutable synthetic request")
    assert effects(tmp_path) == 1
    graph.nodes[1].call.arguments.clear()
    graph.resource_versions["synthetic"] = "1"
    controller.application = original_app
    # The first completed journal result is retained despite the guard failure.
    # Rebuild from the original definition and never repeat its external effect.
    fresh = fixture.harness(tmp_path, parallel=True)
    result = await fresh.resume(context=fixture.context())
    assert result.status is RunStatus.SUCCEEDED
    assert effects(tmp_path) == 2 and result.usage.tool_calls == 2
    with sqlite3.connect(tmp_path / "effects.sqlite") as connection:
        assert [row[0] for row in connection.execute("SELECT value FROM effects")] == [
            "synthetic",
            "synthetic",
        ]


@pytest.mark.parametrize("steps", [0, 2])
async def test_tool_obeying_nested_step_allowance_stays_within_node_limit(
    tmp_path: Path, steps: int
) -> None:
    controller = fixture.harness(tmp_path)
    graph = controller.orchestrator.graph
    node = graph.nodes[0].model_copy(update={"max_usage": Usage(tool_calls=1, steps=steps)})
    controller.orchestrator = replace(
        controller.orchestrator, graph=graph.model_copy(update={"nodes": (node,)})
    )
    granted = []

    async def obey_allowance(arguments, context):
        granted.append(context.allowance.remaining_steps)
        consumed = 1 if context.allowance.remaining_steps > 0 else 0
        return ToolOutput(ok=True, content="obeyed allowance", usage=Usage(steps=consumed))

    controller.application.tools[0].execute = obey_allowance
    result = await controller.run(context=fixture.context(), task="test")
    assert result.status is RunStatus.SUCCEEDED
    assert granted == [steps]
    assert result.usage == Usage(tool_calls=1, steps=min(steps, 1))

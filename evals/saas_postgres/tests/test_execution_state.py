"""PG execution input/checkpoint/evidence transactions, without provider dispatch."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from forgeharness.cloud.calls import PostgresCallStore
from forgeharness.cloud.execution_state import ExecutionStateStore, execution_input_digest
from forgeharness.cloud.models import (
    CallRequest,
    CloudConflict,
    CloudDenied,
    Receipt,
    RunDefinition,
    StaleLease,
    TenantContext,
)
from forgeharness.domain.models import (
    Actor,
    Message,
    MessageRole,
    PendingApproval,
    ResourceScope,
    RunContext,
    RunStatus,
    ToolCall,
    Usage,
)
from forgeharness.runtime.budget import RunBudget

from .test_call_store import database_condition

TASK = "Summarize the authorized tenant material."
INSTRUCTIONS = (Message(role=MessageRole.SYSTEM, content="Use registered capabilities only."),)


def definition(task: str = TASK, instructions: tuple[Message, ...] = INSTRUCTIONS) -> RunDefinition:
    return RunDefinition(
        application_id="synthetic-execution",
        application_version="1",
        policy_version="1",
        binding_digest="a" * 64,
        input_digest=execution_input_digest(task, instructions),
        max_units=10,
        deadline_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def context_factory(owner: TenantContext, **changes) -> Callable[[str], RunContext]:
    def create(run_id: str) -> RunContext:
        return RunContext(
            application_id="synthetic-execution",
            application_version="1",
            policy_version="1",
            run_id=run_id,
            session_id="synthetic-session",
            actor=Actor(id=owner.actor_id),
            scope=ResourceScope(tenant_id=owner.org_id, resources=("material-a",)),
            correlation_id="execution-contract",
        ).model_copy(update=changes)

    return create


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


async def prepared(pgcase):
    owner = await pgcase.seed(actor_id="owner", can_execute=False)
    worker = await pgcase.seed(actor_id="worker")
    state = ExecutionStateStore(pgcase.store)
    run = await state.accept(
        owner,
        "execution-contract",
        definition(),
        task=TASK,
        instructions=INSTRUCTIONS,
        budget=RunBudget(max_attempts_per_call=1),
        create_context=context_factory(owner),
    )
    ticket = await pgcase.store.claim(worker, run.run_id, worker_id="worker-a")
    initial = await state.load(owner, run.run_id)
    evidence = await state.trace(owner, run.run_id)
    return state, owner, worker, run, ticket, initial, evidence


async def test_accept_persists_complete_input_created_checkpoint_and_scheduling_atomically(pgcase):
    owner = await pgcase.seed(actor_id="owner", can_execute=False)
    state, budget, immutable = (
        ExecutionStateStore(pgcase.store),
        RunBudget(max_attempts_per_call=1),
        definition(),
    )

    async def accept():
        return await state.accept(
            owner,
            "atomic-input",
            immutable,
            task=TASK,
            instructions=INSTRUCTIONS,
            budget=budget,
            create_context=context_factory(owner),
        )

    first, duplicate = await asyncio.gather(accept(), accept())
    assert first == duplicate
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.runs") == 1
    row = await pgcase.admin.fetchrow("SELECT * FROM fh_cloud.execution_checkpoints")
    assert decoded(row["initial_input"]) == {
        "task": TASK,
        "instructions": [item.model_dump(mode="json") for item in INSTRUCTIONS],
    }
    assert decoded(row["budget"]) == budget.model_dump(mode="json")
    initial = await state.load(owner, first.run_id)
    assert initial.status is RunStatus.CREATED and initial.checkpoint_revision == 0
    assert initial.messages == (*INSTRUCTIONS, Message(role=MessageRole.USER, content=TASK))
    assert initial.run_context == context_factory(owner)(first.run_id)
    assert decoded(row["owner_context"]) == initial.run_context.model_dump(mode="json")
    assert [item["kind"] for item in await pgcase.store.events(owner, first.run_id)] == [
        "run.queued",
        "execution.accepted",
    ]


async def test_accept_database_failure_rolls_back_run_input_and_both_outbox_events(pgcase):
    owner = await pgcase.seed(actor_id="owner")
    await pgcase.admin.execute(
        "ALTER TABLE fh_cloud.execution_checkpoints ADD CONSTRAINT reject_initial_input "
        "CHECK (checkpoint->>'status' <> 'created')"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await ExecutionStateStore(pgcase.store).accept(
            owner,
            "rollback-input",
            definition(),
            task=TASK,
            instructions=INSTRUCTIONS,
            budget=RunBudget(max_attempts_per_call=1),
            create_context=context_factory(owner),
        )
    for table in ("runs", "execution_checkpoints", "outbox"):
        assert await pgcase.admin.fetchval(f"SELECT COUNT(*) FROM fh_cloud.{table}") == 0


@pytest.mark.parametrize("changed", ["context", "budget", "task", "instructions"])
async def test_idempotent_accept_rejects_different_execution_bindings(pgcase, changed):
    owner = await pgcase.seed(actor_id="owner")
    state, immutable = ExecutionStateStore(pgcase.store), definition()
    options = {
        "task": TASK,
        "instructions": INSTRUCTIONS,
        "budget": RunBudget(max_attempts_per_call=1),
        "create_context": context_factory(owner),
    }
    original = await state.accept(owner, "exact-input", immutable, **options)
    if changed == "context":
        options["create_context"] = context_factory(owner, correlation_id="another-binding")
    elif changed == "budget":
        options["budget"] = RunBudget(max_attempts_per_call=1, max_steps=2)
    elif changed == "task":
        options["task"] = "Different input."
    else:
        options["instructions"] = (Message(role=MessageRole.SYSTEM, content="Changed."),)
    with pytest.raises(CloudConflict):
        await state.accept(owner, "exact-input", immutable, **options)
    assert (await state.load(owner, original.run_id)).messages[-1].content == TASK
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.runs") == 1
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox") == 2


async def test_governance_only_run_cannot_be_adopted_as_an_execution(pgcase):
    owner = await pgcase.seed(actor_id="owner")
    immutable = definition()
    original = await pgcase.store.submit(owner, "governance-only", immutable)
    with pytest.raises(CloudConflict, match="another execution binding"):
        await ExecutionStateStore(pgcase.store).accept(
            owner,
            "governance-only",
            immutable,
            task=TASK,
            instructions=INSTRUCTIONS,
            budget=RunBudget(max_attempts_per_call=1),
            create_context=context_factory(owner),
        )
    assert await pgcase.store.get(owner, original.run_id) == original
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_checkpoints") == 0
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox") == 1


async def test_invalid_trusted_context_rolls_back_new_execution(pgcase):
    owner = await pgcase.seed(actor_id="owner")
    with pytest.raises(CloudDenied):
        await ExecutionStateStore(pgcase.store).accept(
            owner,
            "wrong-owner",
            definition(),
            task=TASK,
            instructions=INSTRUCTIONS,
            budget=RunBudget(max_attempts_per_call=1),
            create_context=context_factory(owner, actor=Actor(id="another-owner")),
        )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.runs") == 0
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox") == 0


async def test_checkpoint_and_complete_hash_chained_trace_commit_together(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("run.started", {"application": "synthetic-execution"})
    evidence.append("context.compiled", {"materials": [{"resource": "material-a", "text": TASK}]})
    candidate = initial.model_copy(update={"status": RunStatus.RUNNING})
    saved = await state.save(worker, ticket, candidate, evidence)
    assert saved.checkpoint_revision == 1 and not evidence.pending()
    assert await state.load(owner, run.run_id) == saved
    restored = await state.trace(owner, run.run_id)
    assert restored.export_json() == evidence.export_json() and not restored.pending()
    row = await pgcase.admin.fetchrow("SELECT * FROM fh_cloud.execution_checkpoints")
    envelopes = json.loads(restored.export_json())
    assert row["trace_sequence"] == len(envelopes) == 2
    assert row["trace_hash"] == envelopes[-1]["hash"]
    assert (await pgcase.store.events(owner, run.run_id))[-1]["kind"] == "execution.checkpoint"


async def test_trace_over_one_transport_batch_restores_all_committed_history(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    for index in range(100):
        evidence.append("context.item", {"index": index, "text": f"material-{index}"})
    saved = await state.save(
        worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
    )
    evidence.append("context.item", {"index": 100, "text": "material-100"})
    saved = await state.save(worker, ticket, saved, evidence)
    assert not evidence.pending()
    restored = await state.trace(owner, run.run_id)
    assert len(restored.events) == 101 and not restored.pending()
    assert restored.export_json() == evidence.export_json()
    restored.append("context.item", {"index": 101, "text": "material-101"})
    await state.save(worker, ticket, saved, restored)
    assert len((await state.trace(owner, run.run_id)).events) == 102
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 102


async def test_checkpoint_failure_keeps_evidence_pending_and_rolls_back_every_projection(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("run.started", {"text": TASK})
    batch = evidence.pending()
    await pgcase.admin.execute(
        "ALTER TABLE fh_cloud.execution_checkpoints ADD CONSTRAINT reject_checkpoint "
        "CHECK(revision=0)"
    )
    before_events = await pgcase.store.events(owner, run.run_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await state.save(
            worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
        )
    assert evidence.pending() == batch
    assert await state.load(owner, run.run_id) == initial
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0
    assert await pgcase.store.events(owner, run.run_id) == before_events
    await pgcase.admin.execute(
        "ALTER TABLE fh_cloud.execution_checkpoints DROP CONSTRAINT reject_checkpoint"
    )
    await state.save(
        worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
    )
    assert not evidence.pending()


async def test_composed_transaction_rollback_does_not_acknowledge_buffer(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("run.started", {"text": TASK})
    batch = evidence.pending()
    with pytest.raises(RuntimeError, match="abort composed transaction"):
        async with pgcase.store.transaction(worker, "execute") as connection:
            await state.save_in_transaction(
                connection,
                worker,
                ticket,
                initial.model_copy(update={"status": RunStatus.RUNNING}),
                batch,
            )
            raise RuntimeError("abort composed transaction")
    assert evidence.pending() == batch
    assert await state.load(owner, run.run_id) == initial
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0


async def test_stale_checkpoint_revision_cannot_append_evidence(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    candidate = initial.model_copy(update={"status": RunStatus.RUNNING})
    saved = await state.save(worker, ticket, candidate, evidence)
    evidence.append("context.item", {"text": "must not commit"})
    with pytest.raises(CloudConflict, match="revision changed"):
        await state.save(worker, ticket, candidate, evidence)
    assert evidence.pending() and await state.load(owner, run.run_id) == saved
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0


async def test_lost_lease_cannot_advance_checkpoint_or_acknowledge_trace(pgcase):
    state, owner, worker, run, old, initial, evidence = await prepared(pgcase)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()-interval '1 second' "
        "WHERE run_id=$1",
        run.run_id,
    )
    current = await pgcase.store.claim(worker, run.run_id, worker_id="replacement")
    assert current.epoch > old.epoch
    evidence.append("run.started", {"text": TASK})
    with pytest.raises(StaleLease):
        await state.save(
            worker, old, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
        )
    assert evidence.pending() and await state.load(owner, run.run_id) == initial
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0


async def test_lease_expiring_while_waiting_for_checkpoint_lock_rolls_back(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    ticket = await pgcase.store.renew(worker, ticket, ttl_seconds=0.5)
    evidence.append("run.started", {"text": TASK})
    async with pgcase.admin.transaction():
        await pgcase.admin.execute("SELECT 1 FROM fh_cloud.execution_checkpoints FOR UPDATE")
        saving = asyncio.create_task(
            state.save(
                worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
            )
        )
        try:
            await database_condition(
                pgcase.admin,
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=$1 "
                "AND wait_event_type='Lock' AND query LIKE '%execution_checkpoints%')",
                pgcase.role,
            )
            await database_condition(pgcase.admin, "SELECT clock_timestamp()>$1", ticket.expires_at)
        except BaseException:
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)
            raise
    with pytest.raises(StaleLease):
        await saving
    assert evidence.pending() and await state.load(owner, run.run_id) == initial
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.AWAITING_APPROVAL])
async def test_unresolved_call_blocks_execution_completion_or_approval_suspension(pgcase, status):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    approver = await pgcase.seed(actor_id="approver", can_execute=False, can_approve=True)
    calls = PostgresCallStore(pgcase.store)
    request = CallRequest(
        invocation_id="pending-call",
        tool_name="synthetic-write",
        effect_class="write",
        arguments_digest="c" * 64,
        reserve_units=1,
    )
    approval = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, approval)
    evidence.append("run.stopping", {"reason": "must retain unresolved reservation"})
    candidate = initial.model_copy(
        update={
            "status": status,
            "pending_approval": PendingApproval(
                call=ToolCall(id="next", name="synthetic-write", arguments={}), reason="approval"
            )
            if status is RunStatus.AWAITING_APPROVAL
            else None,
        }
    )
    with pytest.raises(CloudConflict, match="unresolved"):
        await state.save(worker, ticket, candidate, evidence)
    assert evidence.pending() and await state.load(owner, run.run_id) == initial
    assert (await pgcase.store.get(owner, run.run_id)).reserved_units == 1


@pytest.mark.parametrize("dimension", ["steps", "tool_calls", "input_tokens", "output_tokens"])
async def test_checkpoint_cannot_forge_usage_without_settled_physical_calls(pgcase, dimension):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("run.started", {"text": TASK})
    candidate = initial.model_copy(
        update={"status": RunStatus.RUNNING, "usage": Usage(**{dimension: 1})}
    )
    with pytest.raises(CloudConflict, match="usage differs"):
        await state.save(worker, ticket, candidate, evidence)
    assert evidence.pending() and await state.load(owner, run.run_id) == initial


async def test_empty_execution_cannot_be_reported_as_success(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("run.finished", {"status": "succeeded"})
    with pytest.raises(CloudConflict, match="confirmed execution results"):
        await state.save(
            worker,
            ticket,
            initial.model_copy(update={"status": RunStatus.SUCCEEDED, "final_output": "invented"}),
            evidence,
        )
    assert evidence.pending() and await state.load(owner, run.run_id) == initial
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0


@pytest.mark.parametrize(
    "dimension", ["units", "steps", "tool_calls", "input_tokens", "output_tokens"]
)
async def test_success_checks_settled_budget_before_confirmed_output(pgcase, dimension):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    actual = Usage()
    if dimension == "units":
        await pgcase.admin.execute(
            "UPDATE fh_cloud.runs SET used_units=$1 WHERE run_id=$2",
            run.definition.max_units + 1,
            run.run_id,
        )
    else:
        budget = RunBudget(max_attempts_per_call=1)
        limits = {
            "steps": budget.max_steps,
            "tool_calls": budget.max_tool_calls,
            "input_tokens": budget.max_input_tokens,
            "output_tokens": budget.max_output_tokens,
        }
        actual = Usage(**{dimension: limits[dimension] + 1})
        await pgcase.admin.execute(
            "UPDATE fh_cloud.execution_checkpoints SET used_usage=$1::jsonb WHERE run_id=$2",
            actual.model_dump_json(),
            run.run_id,
        )
    evidence.append("run.finished", {"status": "succeeded"})
    with pytest.raises(CloudConflict, match="exceeded its budget"):
        await state.save(
            worker,
            ticket,
            initial.model_copy(update={"status": RunStatus.SUCCEEDED, "usage": actual}),
            evidence,
        )
    assert evidence.pending() and await state.load(owner, run.run_id) == initial
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0


async def test_cancelled_execution_overrides_a_failed_checkpoint_and_clears_output(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    await pgcase.store.cancel(owner, run.run_id)
    evidence.append("run.finished", {"status": "failed"})
    saved = await state.save(
        worker,
        ticket,
        initial.model_copy(
            update={
                "status": RunStatus.FAILED,
                "final_output": "must not be published",
                "pending_approval": PendingApproval(
                    call=ToolCall(id="old", name="write", arguments={}), reason="obsolete"
                ),
                "authorized_call_id": "old",
            }
        ),
        evidence,
    )
    assert saved.status is RunStatus.CANCELLED
    assert saved.final_output is None and saved.error == "cancelled"
    assert saved.pending_approval is None and saved.authorized_call_id is None
    assert not evidence.pending() and await state.load(owner, run.run_id) == saved
    public = await pgcase.store.get(owner, run.run_id)
    assert public.status == "cancelled" and public.lease_until is None
    last = (await pgcase.store.events(owner, run.run_id))[-1]
    assert last["kind"] == "execution.checkpoint" and last["payload"]["status"] == "cancelled"


async def test_execution_identity_and_foreign_organization_are_unavailable(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    peer = await pgcase.seed(actor_id="peer", can_execute=False)
    foreign = await pgcase.seed(org_id="org-b", actor_id="foreign-worker")
    other_worker = await pgcase.seed(actor_id="other-worker")
    for denied in (peer, foreign):
        with pytest.raises(CloudDenied):
            await state.load(denied, run.run_id)
        with pytest.raises(CloudDenied):
            await state.trace(denied, run.run_id)
    evidence.append("run.started", {"text": TASK})
    for denied in (other_worker, foreign):
        with pytest.raises(StaleLease):
            await state.save(
                denied, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
            )
    assert evidence.pending() and await state.load(owner, run.run_id) == initial
    forged_context = initial.run_context.model_copy(update={"actor": Actor(id="peer")})
    with pytest.raises(CloudConflict, match="binding or revision"):
        await state.save(
            worker,
            ticket,
            initial.model_copy(update={"run_context": forged_context, "status": RunStatus.RUNNING}),
            evidence,
        )


@pytest.mark.parametrize("corruption", ["tail", "envelope", "head"])
async def test_trace_restore_refuses_missing_or_changed_committed_history(pgcase, corruption):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("run.started", {"text": TASK})
    evidence.append("context.compiled", {"resources": ["material-a"]})
    await state.save(
        worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
    )
    if corruption == "tail":
        await pgcase.admin.execute("DELETE FROM fh_cloud.execution_evidence WHERE sequence=2")
    elif corruption == "envelope":
        await pgcase.admin.execute(
            "UPDATE fh_cloud.execution_evidence SET envelope=jsonb_set("
            "envelope,'{event,payload}', '{\"changed\":true}'::jsonb) WHERE sequence=2"
        )
    else:
        await pgcase.admin.execute(
            "UPDATE fh_cloud.execution_checkpoints SET trace_hash=$1", "f" * 64
        )
    with pytest.raises(ValueError, match=r"incomplete|hash|head"):
        await state.trace(owner, run.run_id)


async def test_runtime_has_no_default_tenant_or_evidence_rewrite_permission(pgcase):
    state, _, worker, _, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("run.started", {"text": TASK})
    await state.save(
        worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
    )
    connection = await pgcase.app_connection()
    try:
        for table in ("execution_checkpoints", "execution_evidence", "execution_calls"):
            assert await connection.fetchval(f"SELECT COUNT(*) FROM fh_cloud.{table}") == 0
        async with connection.transaction():
            await connection.execute("SELECT set_config('fh.org_id','org-b',true)")
            assert (
                await connection.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0
            )
        async with connection.transaction():
            await connection.execute("SELECT set_config('fh.org_id',$1,true)", worker.org_id)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute("UPDATE fh_cloud.execution_evidence SET event_hash='bad'")
    finally:
        await connection.close()


async def test_custom_domain_transform_cannot_disable_credential_checks(pgcase):
    class DomainOnly:
        def transform(self, value):
            return value

    owner = await pgcase.seed(actor_id="owner", can_execute=False)
    state = ExecutionStateStore(pgcase.store, transform=DomainOnly())
    task = "Authorization: Bearer synthetic-sensitive-value"
    with pytest.raises(ValueError, match="credentials"):
        await state.accept(
            owner,
            "never-store-credentials",
            definition(task=task),
            task=task,
            instructions=INSTRUCTIONS,
            budget=RunBudget(max_attempts_per_call=1),
            create_context=context_factory(owner),
        )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.runs") == 0
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox") == 0


async def test_trace_read_uses_one_checkpoint_and_history_version(pgcase, monkeypatch):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    evidence.append("first", {})
    current = await state.save(
        worker,
        ticket,
        initial.model_copy(update={"status": RunStatus.RUNNING}),
        evidence,
    )
    evidence.append("second", {})
    entered, release = asyncio.Event(), asyncio.Event()
    history = state._history

    async def paused_history(*args):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await history(*args)

    monkeypatch.setattr(state, "_history", paused_history)
    reader = asyncio.create_task(state.trace(owner, run.run_id))
    writer = None
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        writer = asyncio.create_task(state.save(worker, ticket, current, evidence))
        await database_condition(
            pgcase.admin,
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=$1 "
            "AND wait_event_type='Lock' AND query LIKE '%execution_checkpoints%')",
            pgcase.role,
        )
        release.set()
        old_trace, _ = await asyncio.gather(reader, writer)
        assert len(old_trace.events) == 1
        assert len((await state.trace(owner, run.run_id)).events) == 2
    finally:
        release.set()
        for task in (reader, writer):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *[t for t in (reader, writer) if t is not None], return_exceptions=True
        )


async def test_running_checkpoint_cannot_clear_unknown_reconciliation(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    reviewer = await pgcase.seed(actor_id="reviewer", can_execute=False, can_approve=True)
    calls = PostgresCallStore(pgcase.store)
    request = CallRequest(
        invocation_id="first",
        tool_name="write",
        effect_class="write",
        arguments_digest="b" * 64,
        reserve_units=4,
    )
    second = request.model_copy(update={"invocation_id": "second"})
    first_grant = await calls.grant(reviewer, run.run_id, request)
    second_grant = await calls.grant(reviewer, run.run_id, second)
    await calls.admit(worker, ticket, request, first_grant)
    await calls.mark_sent(worker, ticket, request.invocation_id)
    await calls.mark_unknown(worker, ticket, request.invocation_id)
    await state.save(
        worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
    )
    assert (await pgcase.store.get(owner, run.run_id)).status == "reconciling"
    with pytest.raises(CloudConflict):
        await calls.admit(worker, ticket, second, second_grant)
    assert (await pgcase.store.get(owner, run.run_id)).reserved_units == 4


async def test_approval_can_be_granted_to_a_persistently_suspended_execution(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    proposal = ToolCall(id="proposal", name="write", arguments={"text": "review me"})
    await state.save(
        worker,
        ticket,
        initial.model_copy(
            update={
                "status": RunStatus.AWAITING_APPROVAL,
                "pending_approval": PendingApproval(
                    call=proposal, reason="independent approval required"
                ),
            }
        ),
        evidence,
    )
    reviewer = await pgcase.seed(actor_id="reviewer", can_execute=False, can_approve=True)
    request = CallRequest(
        invocation_id="proposal",
        tool_name="write",
        effect_class="write",
        arguments_digest="b" * 64,
        reserve_units=4,
    )
    grant = await PostgresCallStore(pgcase.store).grant(reviewer, run.run_id, request)
    assert grant
    assert (await pgcase.store.get(owner, run.run_id)).status == "awaiting_approval"
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0


async def test_legacy_finish_cannot_bypass_managed_checkpoint_and_evidence(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    with pytest.raises(CloudConflict, match="managed execution"):
        await pgcase.store.finish(worker, ticket, "failed")
    assert await state.load(owner, run.run_id) == initial
    assert (await pgcase.store.get(owner, run.run_id)).status == "running"
    assert not evidence.pending()


@pytest.mark.parametrize("partial_fact", ["missing", "uncounted"])
async def test_checkpoint_cannot_ignore_settled_call_usage_evidence(pgcase, partial_fact):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    reviewer = await pgcase.seed(actor_id="reviewer", can_execute=False, can_approve=True)
    calls = PostgresCallStore(pgcase.store)
    request = CallRequest(
        invocation_id="fact",
        tool_name="read",
        effect_class="read",
        arguments_digest="b" * 64,
        reserve_units=0,
    )
    grant = await calls.grant(reviewer, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    await calls.mark_sent(worker, ticket, request.invocation_id)
    await calls.record_receipt(
        worker,
        run.run_id,
        request.invocation_id,
        Receipt(receipt_id="fact", actual_units=0, result_digest="c" * 64),
    )
    await calls.settle(worker, ticket, request.invocation_id)
    if partial_fact == "uncounted":
        # Deliberately simulate a faulty/incomplete execution settlement. This
        # fixture tests projection consistency, not actual tool execution.
        await pgcase.admin.execute(
            "INSERT INTO fh_cloud.execution_calls(org_id,run_id,invocation_id,kind,logical_id,"
            "idempotency_key,canonical_input,reserved_usage,actual_usage) "
            "VALUES($1,$2,'fact','tool','fact','fact','{}',$3::jsonb,$3::jsonb)",
            owner.org_id,
            run.run_id,
            Usage(tool_calls=1).model_dump_json(),
        )
    evidence.append("run.finished", {"status": "failed"})
    with pytest.raises(CloudConflict, match=r"full usage evidence|differs from settled facts"):
        await state.save(
            worker, ticket, initial.model_copy(update={"status": RunStatus.FAILED}), evidence
        )
    assert evidence.pending()
    assert await state.load(owner, run.run_id) == initial
    assert (await pgcase.store.get(owner, run.run_id)).status == "running"

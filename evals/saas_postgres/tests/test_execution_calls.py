"""Actual PG reservations, dispatch fences and full physical-result settlement."""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from forgeharness.cloud.calls import PostgresCallStore
from forgeharness.cloud.execution_calls import ExecutionCallStore
from forgeharness.cloud.execution_contract import CallEnvelope, PhysicalResult
from forgeharness.cloud.models import CallRequest, CloudConflict, CloudDenied, Receipt, StaleLease
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    PendingApproval,
    RunStatus,
    ToolCall,
    Usage,
)
from forgeharness.tools.base import RiskLevel, ToolEffectClass, ToolOutput, ToolSpec
from forgeharness.tools.dispatcher import DispatchResult

from .test_call_store import database_condition
from .test_execution_state import prepared


def envelope(kind="tool", logical_id="logical-1", *, reserve_units=2, reservation=None):
    if kind == "tool":
        call = ToolCall(id=logical_id, name="synthetic", arguments={"text": "original"})
        spec = ToolSpec(
            name="synthetic",
            description="Synthetic capability with externally verifiable execution.",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            effect_class=ToolEffectClass.IDEMPOTENT,
            risk=RiskLevel.WRITE,
        )
        canonical = {
            "call": call.model_dump(mode="json"),
            "tool_spec": spec.model_dump(mode="json"),
        }
    else:
        canonical = {"prepared": {"messages": [{"role": "user", "content": "original"}]}}
    return CallEnvelope(
        kind=kind,
        logical_id=logical_id,
        canonical_input=canonical,
        reservation=reservation or (Usage(tool_calls=1) if kind == "tool" else Usage(steps=1)),
        reserve_units=reserve_units,
    )


def request(item):
    return CallRequest(
        invocation_id=item.invocation_id,
        tool_name="synthetic" if item.kind == "tool" else "model.decide",
        effect_class="idempotent_write" if item.kind == "tool" else "read",
        arguments_digest=item.arguments_digest,
        reserve_units=item.reserve_units,
    )


def physical(kind="tool", *, receipt_id="physical-result", actual_units=1, elapsed_ms=7):
    if kind == "tool":
        return PhysicalResult(
            receipt_id=receipt_id,
            actual_units=actual_units,
            kind="tool",
            tool_result=DispatchResult(
                output=ToolOutput(ok=True, content="confirmed original output"),
                elapsed_ms=elapsed_ms,
            ),
        )
    return PhysicalResult(
        receipt_id=receipt_id,
        actual_units=actual_units,
        kind="model",
        model_result=ModelResult(
            action=FinalAction(content="confirmed model output"),
            usage=ModelUsage(input_tokens=3, output_tokens=2),
        ),
    )


async def active(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    current = await state.save(
        worker, ticket, initial.model_copy(update={"status": RunStatus.RUNNING}), evidence
    )
    return ExecutionCallStore(state), owner, worker, run, ticket, current, evidence


@pytest.mark.parametrize("kind", ["model", "tool"])
async def test_policy_admission_full_result_and_all_dimensions_settle_once(pgcase, kind):
    calls, owner, worker, run, ticket, current, evidence = await active(pgcase)
    item = envelope(kind)
    evidence.append("call.prepared", {"canonical_input": item.canonical_input})
    batch = evidence.snapshot_pending()
    admitted, saved = await calls.admit(
        worker,
        ticket,
        item,
        request=request(item),
        expected_revision=current.checkpoint_revision,
        batch=batch,
    )
    assert admitted.state == "admitted" and saved is None
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.approvals") == 0
    assert (
        await calls.state.load(owner, run.run_id)
    ).checkpoint_revision == current.checkpoint_revision
    assert evidence.snapshot_pending() == batch
    evidence.ack_committed(batch)
    await calls.mark_sent(worker, ticket, item.invocation_id)
    result = physical(kind)
    await calls.record_result(worker, run.run_id, item.invocation_id, result)
    await calls.settle(worker, ticket, item.invocation_id)
    await calls.settle(worker, ticket, item.invocation_id)
    fact = await calls.get(worker, run.run_id, item.invocation_id)
    assert fact.invocation.state == "settled" and fact.result == result
    assert fact.authorization_mode == "policy" and fact.envelope == item
    assert fact.idempotency_key == item.idempotency_key(
        owner.org_id, run.run_id, run.definition.binding_digest
    )
    public = await pgcase.store.get(owner, run.run_id)
    assert public.used_units == result.actual_units and public.reserved_units == 0
    row = await pgcase.admin.fetchrow(
        "SELECT used_usage,reserved_usage FROM fh_cloud.execution_checkpoints"
    )
    assert json.loads(row["used_usage"]) == result.actual_usage.model_dump(mode="json")
    assert not any(json.loads(row["reserved_usage"]).values())
    current = current.model_copy(
        update={"status": RunStatus.SUCCEEDED, "usage": result.actual_usage}
    )
    assert (await calls.state.save(worker, ticket, current, evidence)).status is RunStatus.SUCCEEDED


async def test_duplicate_admission_does_not_reserve_or_consume_twice(pgcase):
    calls, _, worker, _, ticket, current, _ = await active(pgcase)
    item = envelope()
    admitted = await asyncio.gather(
        *(
            calls.admit(
                worker,
                ticket,
                item,
                request=request(item),
                expected_revision=current.checkpoint_revision,
            )
            for _ in range(2)
        )
    )
    assert admitted[0] == admitted[1]
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_calls") == 1
    assert (
        await pgcase.admin.fetchval("SELECT reserved_units FROM fh_cloud.runs")
        == item.reserve_units
    )


async def test_admission_failure_rolls_back_full_input_reservations_evidence_and_outbox(pgcase):
    calls, owner, worker, run, ticket, current, evidence = await active(pgcase)
    item = envelope()
    evidence.append("call.prepared", {"canonical_input": item.canonical_input})
    batch = evidence.snapshot_pending()
    await pgcase.admin.execute(
        "ALTER TABLE fh_cloud.execution_checkpoints ADD CONSTRAINT reject_reserved_usage "
        "CHECK(reserved_usage->>'tool_calls'='0')"
    )
    before = await pgcase.store.events(owner, run.run_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await calls.admit(
            worker,
            ticket,
            item,
            request=request(item),
            expected_revision=current.checkpoint_revision,
            batch=batch,
        )
    assert evidence.snapshot_pending() == batch
    for table in ("invocations", "execution_calls", "execution_evidence"):
        assert await pgcase.admin.fetchval(f"SELECT COUNT(*) FROM fh_cloud.{table}") == 0
    assert await pgcase.admin.fetchval("SELECT reserved_units FROM fh_cloud.runs") == 0
    assert await pgcase.store.events(owner, run.run_id) == before


async def test_exact_approval_consumption_reservation_and_checkpoint_are_atomic(pgcase):
    calls, owner, worker, run, ticket, current, evidence = await active(pgcase)
    item = envelope()
    pending = ToolCall.model_validate(item.canonical_input["call"])
    suspended = await calls.state.save(
        worker,
        ticket,
        current.model_copy(
            update={
                "status": RunStatus.AWAITING_APPROVAL,
                "pending_approval": PendingApproval(call=pending, reason="independent approval"),
            }
        ),
        evidence,
    )
    approver = await pgcase.seed(actor_id="approver", can_execute=False, can_approve=True)
    approval = await PostgresCallStore(pgcase.store).grant(approver, run.run_id, request(item))
    ticket = await pgcase.store.claim(worker, run.run_id, worker_id="approved-worker")
    candidate = suspended.model_copy(
        update={
            "status": RunStatus.RUNNING,
            "pending_approval": None,
            "authorized_call_id": pending.id,
        }
    )
    admitted, saved = await calls.admit(
        worker,
        ticket,
        item,
        request=request(item),
        approval_id=approval,
        checkpoint=candidate,
        expected_revision=suspended.checkpoint_revision,
    )
    assert (
        admitted.state == "admitted"
        and saved.checkpoint_revision == suspended.checkpoint_revision + 1
    )
    assert await calls.state.load(owner, run.run_id) == saved
    assert await pgcase.admin.fetchval("SELECT consumed FROM fh_cloud.approvals")
    await calls.mark_sent(worker, ticket, item.invocation_id)


@pytest.mark.parametrize("dimension", ["steps", "tool_calls", "input_tokens", "output_tokens"])
async def test_every_usage_reservation_dimension_is_enforced_before_admission(pgcase, dimension):
    calls, _, worker, _, ticket, current, _ = await active(pgcase)
    limits = {"steps": 12, "tool_calls": 20, "input_tokens": 100_000, "output_tokens": 20_000}
    item = envelope(reservation=Usage(**{dimension: limits[dimension] + 1}))
    with pytest.raises(CloudConflict, match="budget"):
        await calls.admit(
            worker,
            ticket,
            item,
            request=request(item),
            expected_revision=current.checkpoint_revision,
        )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0


async def test_started_call_cannot_be_sent_twice_or_cleared_without_result(pgcase):
    calls, _, worker, _, ticket, current, _ = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await calls.mark_sent(worker, ticket, item.invocation_id)
    with pytest.raises(CloudConflict):
        await calls.mark_sent(worker, ticket, item.invocation_id)
    await calls.mark_unknown(worker, ticket, item.invocation_id)
    with pytest.raises(CloudConflict, match="unresolved"):
        await calls.settle(worker, ticket, item.invocation_id)
    fact = await calls.get(worker, ticket.run_id, item.invocation_id)
    assert fact.invocation.state == "unknown" and fact.result is None
    assert (
        await pgcase.admin.fetchval("SELECT reserved_units FROM fh_cloud.runs")
        == item.reserve_units
    )


async def test_admitted_call_cannot_claim_a_physical_result_before_send(pgcase):
    calls, _, worker, run, ticket, current, _ = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    with pytest.raises(CloudConflict, match="unstarted"):
        await calls.record_result(worker, run.run_id, item.invocation_id, physical())
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.receipts") == 0


async def test_trusted_nonexecution_releases_reservations_without_inventing_output(pgcase):
    calls, _, worker, run, ticket, current, _ = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await PostgresCallStore(pgcase.store).record_receipt(
        worker,
        run.run_id,
        item.invocation_id,
        Receipt(
            receipt_id="verified-not-sent",
            actual_units=0,
            result_digest="e" * 64,
            outcome="not_executed",
        ),
    )
    with pytest.raises(CloudConflict):
        await calls.mark_sent(worker, ticket, item.invocation_id)
    await calls.settle(worker, ticket, item.invocation_id)
    fact = await calls.get(worker, run.run_id, item.invocation_id)
    assert fact.result is None and fact.receipt.outcome == "not_executed"
    assert await pgcase.admin.fetchval("SELECT reserved_units FROM fh_cloud.runs") == 0
    assert json.loads(
        await pgcase.admin.fetchval("SELECT actual_usage FROM fh_cloud.execution_calls")
    ) == Usage().model_dump(mode="json")


async def test_late_physical_result_survives_takeover_and_settles_only_by_current_epoch(pgcase):
    calls, owner, worker, run, old, current, _ = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, old, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await calls.mark_sent(worker, old, item.invocation_id)
    await calls.mark_unknown(worker, old, item.invocation_id)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()-interval '1 second'"
    )
    next_worker = await pgcase.seed(actor_id="next-worker")
    ticket = await pgcase.store.claim(next_worker, run.run_id, worker_id="replacement")
    result = physical(actual_units=12)
    await calls.record_result(worker, run.run_id, item.invocation_id, result)
    with pytest.raises(StaleLease):
        await calls.settle(worker, old, item.invocation_id)
    await calls.settle(next_worker, ticket, item.invocation_id)
    public = await pgcase.store.get(owner, run.run_id)
    assert public.status == "reconciling" and public.used_units == 12 and public.reserved_units == 0
    assert json.loads(
        await pgcase.admin.fetchval("SELECT used_usage FROM fh_cloud.execution_checkpoints")
    ) == result.actual_usage.model_dump(mode="json")


async def test_result_replay_ignores_only_observation_time_and_preserves_first_payload(pgcase):
    calls, _, worker, run, ticket, current, _ = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await calls.mark_sent(worker, ticket, item.invocation_id)
    original = physical(elapsed_ms=19)
    await calls.record_result(worker, run.run_id, item.invocation_id, original)
    await calls.record_result(worker, run.run_id, item.invocation_id, physical(elapsed_ms=0))
    assert (await calls.get(worker, run.run_id, item.invocation_id)).result == original
    with pytest.raises(CloudConflict):
        await calls.record_result(worker, run.run_id, item.invocation_id, physical(actual_units=3))


async def test_revoked_owner_blocks_sending_but_keeps_authenticated_actual_cost(pgcase):
    calls, owner, worker, run, ticket, current, _ = await active(pgcase)
    first, second = envelope(logical_id="first"), envelope(logical_id="second")
    for item in (first, second):
        await calls.admit(
            worker,
            ticket,
            item,
            request=request(item),
            expected_revision=current.checkpoint_revision,
        )
    await calls.mark_sent(worker, ticket, first.invocation_id)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.members SET active=false WHERE actor_id=$1", owner.actor_id
    )
    with pytest.raises(CloudDenied):
        await calls.mark_sent(worker, ticket, second.invocation_id)
    await calls.record_result(worker, run.run_id, first.invocation_id, physical())
    await calls.settle(worker, ticket, first.invocation_id)
    assert await pgcase.admin.fetchval("SELECT used_units FROM fh_cloud.runs") == 1


async def test_known_actual_overspend_blocks_a_previously_admitted_send(pgcase):
    calls, _, worker, run, ticket, current, _ = await active(pgcase)
    first, second = envelope(logical_id="first"), envelope(logical_id="second")
    for item in (first, second):
        await calls.admit(
            worker,
            ticket,
            item,
            request=request(item),
            expected_revision=current.checkpoint_revision,
        )
    await calls.mark_sent(worker, ticket, first.invocation_id)
    await calls.record_result(worker, run.run_id, first.invocation_id, physical(actual_units=12))
    await calls.settle(worker, ticket, first.invocation_id)
    with pytest.raises(CloudConflict, match="budget"):
        await calls.mark_sent(worker, ticket, second.invocation_id)


async def test_existing_governance_admit_still_requires_independent_approval(pgcase):
    _, _, worker, _, ticket, _, _ = await active(pgcase)
    item = envelope()
    with pytest.raises(CloudDenied):
        await PostgresCallStore(pgcase.store).admit(worker, ticket, request(item), "missing")
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0


async def test_fact_absence_cannot_hide_a_governance_only_invocation(pgcase):
    calls, _, worker, run, ticket, _, _ = await active(pgcase)
    approver = await pgcase.seed(actor_id="approver", can_execute=False, can_approve=True)
    item = envelope()
    governance = PostgresCallStore(pgcase.store)
    approval = await governance.grant(approver, run.run_id, request(item))
    await governance.admit(worker, ticket, request(item), approval)
    with pytest.raises(CloudConflict, match="full execution identity"):
        await calls.get(worker, run.run_id, item.invocation_id)


@pytest.mark.parametrize("corruption", ["receipt", "usage"])
async def test_fact_read_refuses_a_result_without_matching_receipt_or_usage(pgcase, corruption):
    calls, _, worker, run, ticket, current, _ = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await calls.mark_sent(worker, ticket, item.invocation_id)
    await calls.record_result(worker, run.run_id, item.invocation_id, physical())
    if corruption == "receipt":
        await pgcase.admin.execute("DELETE FROM fh_cloud.receipts")
    else:
        await pgcase.admin.execute(
            "UPDATE fh_cloud.execution_calls SET actual_usage=$1::jsonb", Usage().model_dump_json()
        )
    with pytest.raises(CloudConflict, match="matching receipt or usage"):
        await calls.get(worker, run.run_id, item.invocation_id)


@pytest.mark.parametrize("operation", ["admit", "mark_sent"])
async def test_approval_expiring_during_evidence_lock_wait_rolls_back(pgcase, operation):
    calls, _, worker, run, ticket, current, evidence = await active(pgcase)
    approver = await pgcase.seed(actor_id="approver", can_execute=False, can_approve=True)
    item = envelope()
    approval = await PostgresCallStore(pgcase.store).grant(
        approver, run.run_id, request(item), ttl_seconds=1
    )
    if operation == "mark_sent":
        await calls.admit(
            worker,
            ticket,
            item,
            request=request(item),
            approval_id=approval,
            expected_revision=current.checkpoint_revision,
        )
    evidence.append("call.prepared", {"canonical_input": item.canonical_input})
    async with pgcase.admin.transaction():
        await pgcase.admin.execute("LOCK TABLE fh_cloud.execution_evidence IN SHARE MODE")
        if operation == "admit":
            future = calls.admit(
                worker,
                ticket,
                item,
                request=request(item),
                approval_id=approval,
                expected_revision=current.checkpoint_revision,
                batch=evidence.snapshot_pending(),
            )
        else:
            future = calls.mark_sent(
                worker, ticket, item.invocation_id, batch=evidence.snapshot_pending()
            )
        pending = asyncio.create_task(future)
        try:
            await database_condition(
                pgcase.admin,
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=$1 "
                "AND wait_event_type='Lock' "
                "AND query LIKE '%INSERT INTO fh_cloud.execution_evidence%')",
                pgcase.role,
            )
            await database_condition(
                pgcase.admin,
                "SELECT clock_timestamp()>expires_at FROM fh_cloud.approvals WHERE approval_id=$1",
                approval,
            )
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise
    with pytest.raises(CloudDenied, match="approval"):
        await pending
    assert evidence.snapshot_pending()
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0
    if operation == "admit":
        assert not await pgcase.admin.fetchval("SELECT consumed FROM fh_cloud.approvals")
        assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0
    else:
        assert await pgcase.admin.fetchval("SELECT state FROM fh_cloud.invocations") == "admitted"

"""A new lease may retire only an older admission that never permitted a send."""

import json

import asyncpg
import pytest

from forgeharness.cloud.models import CloudConflict, StaleLease
from forgeharness.domain.models import RunStatus, Usage

from .test_execution_calls import active, envelope, request


async def admitted(pgcase):
    calls, owner, worker, run, ticket, current, evidence = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker,
        ticket,
        item,
        request=request(item),
        expected_revision=current.checkpoint_revision,
    )
    return calls, owner, worker, run, ticket, current, evidence, item


async def replace_lease(pgcase, worker, run):
    await pgcase.admin.execute(
        "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()-interval '1 second' "
        "WHERE org_id=$1 AND run_id=$2",
        worker.org_id,
        run.run_id,
    )
    return await pgcase.store.claim(worker, run.run_id, "replacement")


async def test_retire_fenced_admission_atomically_proves_no_send_and_releases_budget(pgcase):
    calls, owner, worker, run, old, current, evidence, item = await admitted(pgcase)
    new = await replace_lease(pgcase, worker, run)
    evidence.append("execution.call.unsent", {"invocation_id": item.invocation_id})
    batch = evidence.snapshot_pending()

    retired = await calls.retire_admission(worker, new, item.invocation_id, batch=batch)

    assert retired.state == "settled"
    fact = await calls.get(worker, run.run_id, item.invocation_id)
    assert fact.result is None
    assert fact.receipt.outcome == "not_executed" and fact.receipt.actual_units == 0
    view = await pgcase.store.get(owner, run.run_id)
    assert view.used_units == view.reserved_units == 0
    state = await pgcase.admin.fetchrow(
        "SELECT used_usage,reserved_usage,trace_sequence FROM fh_cloud.execution_checkpoints"
    )
    assert json.loads(state["used_usage"]) == Usage().model_dump(mode="json")
    assert not any(json.loads(state["reserved_usage"]).values())
    assert state["trace_sequence"] == len(batch)
    with pytest.raises(StaleLease):
        await calls.mark_sent(worker, old, item.invocation_id)
    evidence.ack_committed(batch)
    failed = await calls.state.save(
        worker,
        new,
        current.model_copy(update={"status": RunStatus.FAILED, "error": "confirmed unsent"}),
        evidence,
    )
    assert failed.usage == Usage() and failed.status is RunStatus.FAILED
    events = await pgcase.store.events(owner, run.run_id)
    assert any(event["kind"] == "execution.admission_retired" for event in events)


async def test_current_epoch_admission_cannot_be_retired(pgcase):
    calls, _, worker, run, ticket, _, _, item = await admitted(pgcase)

    with pytest.raises(CloudConflict, match="older unsent"):
        await calls.retire_admission(worker, ticket, item.invocation_id)

    fact = await calls.get(worker, run.run_id, item.invocation_id)
    assert fact.invocation.state == "admitted" and fact.receipt is None
    assert await pgcase.admin.fetchval("SELECT reserved_units FROM fh_cloud.runs") == 2


@pytest.mark.parametrize("state", ["sent", "unknown"])
async def test_send_or_unknown_is_never_inferred_to_be_free_nonexecution(pgcase, state):
    calls, _, worker, run, old, _, _, item = await admitted(pgcase)
    await calls.mark_sent(worker, old, item.invocation_id)
    if state == "unknown":
        await calls.mark_unknown(worker, old, item.invocation_id)
    new = await replace_lease(pgcase, worker, run)

    with pytest.raises(CloudConflict, match="older unsent"):
        await calls.retire_admission(worker, new, item.invocation_id)

    fact = await calls.get(worker, run.run_id, item.invocation_id)
    assert fact.invocation.state == state and fact.receipt is None
    assert await pgcase.admin.fetchval("SELECT reserved_units FROM fh_cloud.runs") == 2


async def test_retirement_failure_rolls_back_proof_budget_evidence_and_outbox(pgcase):
    calls, owner, worker, run, _, _, evidence, item = await admitted(pgcase)
    new = await replace_lease(pgcase, worker, run)
    await pgcase.admin.execute(
        "ALTER TABLE fh_cloud.invocations ADD CONSTRAINT no_retirement CHECK(state<>'settled')"
    )
    evidence.append("execution.call.unsent", {"invocation_id": item.invocation_id})
    batch = evidence.snapshot_pending()
    events = await pgcase.store.events(owner, run.run_id)

    with pytest.raises(asyncpg.CheckViolationError):
        await calls.retire_admission(worker, new, item.invocation_id, batch=batch)

    fact = await calls.get(worker, run.run_id, item.invocation_id)
    assert fact.invocation.state == "admitted" and fact.receipt is None
    assert await pgcase.admin.fetchval("SELECT reserved_units FROM fh_cloud.runs") == 2
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0
    assert await pgcase.store.events(owner, run.run_id) == events
    assert evidence.snapshot_pending() == batch

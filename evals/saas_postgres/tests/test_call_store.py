"""Exact approval, bounded admission, unknown usage and receipt settlement."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from forgeharness.cloud.calls import PostgresCallStore
from forgeharness.cloud.models import (
    CallRecord,
    CallRequest,
    CloudConflict,
    CloudDenied,
    Receipt,
    StaleLease,
)

from .test_run_store import definition


def call(invocation_id: str = "call-a", *, reserve_units: int = 4) -> CallRequest:
    return CallRequest(
        invocation_id=invocation_id,
        tool_name="synthetic-write",
        effect_class="idempotent_write",
        arguments_digest="c" * 64,
        reserve_units=reserve_units,
    )


async def prepared(pgcase, **limits):
    owner = await pgcase.seed(actor_id="owner", can_execute=False)
    worker = await pgcase.seed(actor_id="worker")
    approver = await pgcase.seed(actor_id="approver", can_execute=False, can_approve=True)
    run = await pgcase.store.submit(owner, "call-contract", definition(**limits))
    ticket = await pgcase.store.claim(worker, run.run_id, worker_id="worker-a")
    return PostgresCallStore(pgcase.store), owner, worker, approver, run, ticket


async def database_condition(connection, query, *args):
    async with asyncio.timeout(5):
        # Server lock/time state has no in-process asyncio.Event to subscribe to.
        while True:
            # pg_stat_activity caches a snapshot within this observer's open
            # transaction; refresh it before observing a newly blocked worker.
            await connection.execute("SELECT pg_stat_clear_snapshot()")
            if await connection.fetchval(query, *args):
                return
            await asyncio.sleep(0.01)


async def test_approval_mutation_rolls_back_without_consuming_grant_or_reserving_units(pgcase):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    approval = await calls.grant(approver, run.run_id, request)
    changed = request.model_copy(update={"arguments_digest": "d" * 64})
    with pytest.raises((CloudConflict, CloudDenied)):
        await calls.admit(worker, ticket, changed, approval)
    assert not await pgcase.admin.fetchval(
        "SELECT consumed FROM fh_cloud.approvals WHERE approval_id=$1", approval
    )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0
    assert (await pgcase.store.get(owner, run.run_id)).reserved_units == 0
    await calls.admit(worker, ticket, request, approval)
    assert (await pgcase.store.get(owner, run.run_id)).reserved_units == 4


async def test_duplicate_approved_admission_reserves_once_under_concurrency(pgcase):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    approval = await calls.grant(approver, run.run_id, request)
    admitted = await asyncio.gather(
        calls.admit(worker, ticket, request, approval),
        calls.admit(worker, ticket, request, approval),
    )
    assert admitted[0] == admitted[1]
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 1
    assert (await pgcase.store.get(owner, run.run_id)).reserved_units == 4


async def test_parallel_calls_cannot_both_take_the_last_budget(pgcase):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase, max_units=10)
    requests = [call("budget-a", reserve_units=6), call("budget-b", reserve_units=6)]
    grants = [await calls.grant(approver, run.run_id, request) for request in requests]
    outcomes = await asyncio.gather(
        *(
            calls.admit(worker, ticket, request, grant)
            for request, grant in zip(requests, grants, strict=True)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CallRecord) for result in outcomes) == 1
    assert sum(isinstance(result, CloudConflict) for result in outcomes) == 1
    current = await pgcase.store.get(owner, run.run_id)
    assert current.reserved_units == 6 and current.used_units == 0
    assert (
        await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.approvals WHERE consumed") == 1
    )


async def test_call_limit_applies_even_to_zero_unit_calls(pgcase):
    calls, _, worker, approver, run, ticket = await prepared(pgcase, max_calls=1)
    first, second = call("zero-a", reserve_units=0), call("zero-b", reserve_units=0)
    grant_a = await calls.grant(approver, run.run_id, first)
    grant_b = await calls.grant(approver, run.run_id, second)
    await calls.admit(worker, ticket, first, grant_a)
    with pytest.raises(CloudConflict):
        await calls.admit(worker, ticket, second, grant_b)
    assert not await pgcase.admin.fetchval(
        "SELECT consumed FROM fh_cloud.approvals WHERE approval_id=$1", grant_b
    )


async def test_requester_cannot_approve_its_own_call_even_with_both_roles(pgcase):
    calls, owner, worker, _, run, _ = await prepared(pgcase)
    await pgcase.seed(actor_id=owner.actor_id, can_execute=False, can_approve=True)
    with pytest.raises(CloudDenied):
        await calls.grant(owner, run.run_id, call())
    with pytest.raises(CloudDenied):
        await calls.grant(worker, run.run_id, call())


async def test_revoked_or_expired_approver_cannot_authorize_a_call(pgcase):
    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.members SET can_approve=false WHERE org_id=$1 AND actor_id=$2",
        approver.org_id,
        approver.actor_id,
    )
    with pytest.raises((CloudConflict, CloudDenied)):
        await calls.admit(worker, ticket, request, grant)
    await pgcase.seed(actor_id=approver.actor_id, can_execute=False, can_approve=True)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.approvals SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE approval_id=$1",
        grant,
    )
    with pytest.raises((CloudConflict, CloudDenied)):
        await calls.admit(worker, ticket, request, grant)
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0


async def test_unknown_reservation_survives_takeover_and_late_receipt_is_settled_once(pgcase):
    calls, owner, worker, approver, run, old = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, old, request, grant)
    await calls.mark_sent(worker, old, request.invocation_id)
    await calls.mark_unknown(worker, old, request.invocation_id)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()-interval '1 second' "
        "WHERE org_id=$1 AND run_id=$2",
        owner.org_id,
        run.run_id,
    )
    next_worker = await pgcase.seed(actor_id="next-worker")
    current = await pgcase.store.claim(next_worker, run.run_id, worker_id="worker-b")
    state = await pgcase.store.get(owner, run.run_id)
    assert state.status == "reconciling" and state.reserved_units == 4
    with pytest.raises(CloudConflict):
        await calls.mark_sent(next_worker, current, request.invocation_id)
    with pytest.raises(CloudConflict):
        await pgcase.store.finish(next_worker, current, status="failed")
    receipt = Receipt(receipt_id="late-receipt", actual_units=3, result_digest="e" * 64)
    # Old worker may supply an authenticated fact, but may not write the new projection.
    await calls.record_receipt(worker, run.run_id, request.invocation_id, receipt)
    with pytest.raises(CloudConflict):
        await calls.settle(worker, old, request.invocation_id)
    await calls.settle(next_worker, current, request.invocation_id)
    await calls.record_receipt(worker, run.run_id, request.invocation_id, receipt)
    await calls.settle(next_worker, current, request.invocation_id)
    state = await pgcase.store.get(owner, run.run_id)
    assert state.used_units == 3 and state.reserved_units == 0
    await pgcase.store.finish(next_worker, current, status="succeeded")
    assert (await pgcase.store.get(owner, run.run_id)).status == "succeeded"


async def test_conflicting_receipt_never_replaces_the_first_fact(pgcase):
    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    await calls.mark_sent(worker, ticket, request.invocation_id)
    receipt = Receipt(receipt_id="original", actual_units=3, result_digest="e" * 64)
    await calls.record_receipt(worker, run.run_id, request.invocation_id, receipt)
    with pytest.raises(CloudConflict):
        await calls.record_receipt(
            worker,
            run.run_id,
            request.invocation_id,
            receipt.model_copy(update={"actual_units": 4}),
        )
    row = await pgcase.admin.fetchrow("SELECT actual_units,receipt_id FROM fh_cloud.receipts")
    assert row["actual_units"] == 3 and row["receipt_id"] == "original"


async def test_cancel_or_revoke_stops_new_calls_but_retains_real_cost(pgcase):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    next_request = call("not-sent")
    next_grant = await calls.grant(approver, run.run_id, next_request)
    await calls.admit(worker, ticket, request, grant)
    await calls.mark_sent(worker, ticket, request.invocation_id)
    await pgcase.store.cancel(owner, run.run_id)
    with pytest.raises((CloudConflict, CloudDenied)):
        await calls.admit(worker, ticket, next_request, next_grant)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.members SET active=false WHERE org_id=$1 AND actor_id=$2",
        owner.org_id,
        owner.actor_id,
    )
    await pgcase.admin.execute(
        "UPDATE fh_cloud.organizations SET active=false WHERE org_id=$1", owner.org_id
    )
    await calls.record_receipt(
        worker,
        run.run_id,
        request.invocation_id,
        Receipt(receipt_id="after-revoke", actual_units=7, result_digest="f" * 64),
    )
    await calls.settle(worker, ticket, request.invocation_id)
    row = await pgcase.admin.fetchrow("SELECT used_units,reserved_units FROM fh_cloud.runs")
    assert row["used_units"] == 7 and row["reserved_units"] == 0


async def test_actual_overspend_is_recorded_and_blocks_further_admission(pgcase):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase, max_units=10)
    request, next_request = call(), call("after-overspend", reserve_units=0)
    grant = await calls.grant(approver, run.run_id, request)
    next_grant = await calls.grant(approver, run.run_id, next_request)
    await calls.admit(worker, ticket, request, grant)
    await calls.mark_sent(worker, ticket, request.invocation_id)
    await calls.record_receipt(
        worker,
        run.run_id,
        request.invocation_id,
        Receipt(receipt_id="overspent", actual_units=17, result_digest="e" * 64),
    )
    await calls.settle(worker, ticket, request.invocation_id)
    state = await pgcase.store.get(owner, run.run_id)
    assert state.used_units == 17 and state.reserved_units == 0
    with pytest.raises(CloudConflict):
        await calls.admit(worker, ticket, next_request, next_grant)
    with pytest.raises(CloudConflict):
        await pgcase.store.finish(worker, ticket, status="succeeded")


async def test_nonexecution_receipt_is_not_successful_business_completion(pgcase):
    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    await calls.mark_sent(worker, ticket, request.invocation_id)
    await calls.record_receipt(
        worker,
        run.run_id,
        request.invocation_id,
        Receipt(
            receipt_id="not-executed",
            actual_units=0,
            result_digest="e" * 64,
            outcome="not_executed",
        ),
    )
    await calls.settle(worker, ticket, request.invocation_id)
    with pytest.raises(CloudConflict):
        await pgcase.store.finish(worker, ticket, status="succeeded")


@pytest.mark.parametrize(
    "change", [{"tool_name": "different-write"}, {"effect_class": "write"}, {"reserve_units": 5}]
)
async def test_approval_binds_tool_effect_and_exact_reservation(pgcase, change):
    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    with pytest.raises(CloudDenied):
        await calls.admit(worker, ticket, request.model_copy(update=change), grant)
    assert not await pgcase.admin.fetchval(
        "SELECT consumed FROM fh_cloud.approvals WHERE approval_id=$1", grant
    )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0


@pytest.mark.parametrize("revoked", ["owner", "approver", "expired"])
async def test_revocation_after_admission_prevents_the_first_send(pgcase, revoked):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    if revoked == "expired":
        await pgcase.admin.execute(
            "UPDATE fh_cloud.approvals SET expires_at=clock_timestamp()-interval '1 second'"
        )
    else:
        target = owner.actor_id if revoked == "owner" else approver.actor_id
        await pgcase.admin.execute(
            "UPDATE fh_cloud.members SET active=false WHERE org_id=$1 AND actor_id=$2",
            owner.org_id,
            target,
        )
    with pytest.raises(CloudDenied):
        await calls.mark_sent(worker, ticket, request.invocation_id)
    row = await pgcase.admin.fetchrow("SELECT state,reserve_units FROM fh_cloud.invocations")
    assert row["state"] == "admitted" and row["reserve_units"] == 4


async def test_admission_failure_rolls_back_approval_reservation_and_event(pgcase):
    import asyncpg

    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await pgcase.admin.execute(
        "ALTER TABLE fh_cloud.runs ADD CONSTRAINT injected_failure CHECK (reserved_units=0)"
    )
    before = await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox")
    with pytest.raises(asyncpg.CheckViolationError):
        await calls.admit(worker, ticket, request, grant)
    assert not await pgcase.admin.fetchval(
        "SELECT consumed FROM fh_cloud.approvals WHERE approval_id=$1", grant
    )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox") == before
    await pgcase.admin.execute("ALTER TABLE fh_cloud.runs DROP CONSTRAINT injected_failure")
    await calls.admit(worker, ticket, request, grant)


async def test_runtime_cannot_rewrite_recorded_receipts(pgcase):
    import asyncpg

    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    await calls.mark_sent(worker, ticket, request.invocation_id)
    await calls.record_receipt(
        worker,
        run.run_id,
        request.invocation_id,
        Receipt(receipt_id="immutable", actual_units=3, result_digest="f" * 64),
    )
    connection = await pgcase.app_connection()
    try:
        async with connection.transaction():
            await connection.execute("SELECT set_config('fh.org_id',$1,true)", worker.org_id)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute("UPDATE fh_cloud.receipts SET actual_units=0")
    finally:
        await connection.close()


async def test_admitted_call_cannot_claim_a_completed_external_effect(pgcase):
    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    with pytest.raises(CloudConflict):
        await calls.record_receipt(
            worker,
            run.run_id,
            request.invocation_id,
            Receipt(receipt_id="fiction", actual_units=1, result_digest="a" * 64),
        )
    await calls.record_receipt(
        worker,
        run.run_id,
        request.invocation_id,
        Receipt(
            receipt_id="confirmed-nonexecution",
            actual_units=0,
            result_digest="f" * 64,
            outcome="not_executed",
        ),
    )
    await calls.settle(worker, ticket, request.invocation_id)
    finished = await pgcase.store.finish(worker, ticket, status="failed")
    assert finished.status == "failed" and finished.reserved_units == 0


async def test_confirmed_nonexecution_fences_a_later_send(pgcase):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    receipt = Receipt(
        receipt_id="final-nonexecution",
        actual_units=0,
        result_digest="f" * 64,
        outcome="not_executed",
    )
    await calls.record_receipt(worker, run.run_id, request.invocation_id, receipt)
    await calls.record_receipt(worker, run.run_id, request.invocation_id, receipt)
    with pytest.raises(CloudConflict, match="final receipt"):
        await calls.mark_sent(worker, ticket, request.invocation_id)
    assert await pgcase.admin.fetchval("SELECT state FROM fh_cloud.invocations") == "admitted"
    assert (await pgcase.store.get(owner, run.run_id)).reserved_units == 4
    events = await pgcase.store.events(owner, run.run_id)
    assert sum(event["kind"] == "receipt.recorded" for event in events) == 1
    assert not any(event["kind"] == "call.sent" for event in events)


async def test_send_waits_for_receipt_row_lock_then_rechecks_committed_fact(pgcase):
    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    # Hold the same row lock as trusted receipt ingestion. Wait for the sender's
    # actual PostgreSQL lock wait, then commit the receipt before it can proceed.
    async with pgcase.admin.transaction():
        await pgcase.admin.execute("SELECT 1 FROM fh_cloud.invocations FOR UPDATE")
        sender = asyncio.create_task(calls.mark_sent(worker, ticket, request.invocation_id))
        try:
            async with asyncio.timeout(5):
                await database_condition(
                    pgcase.admin,
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=$1 "
                    "AND wait_event_type='Lock' AND query LIKE '%fh_cloud.invocations%')",
                    pgcase.role,
                )
            await pgcase.admin.execute(
                "INSERT INTO fh_cloud.receipts "
                "(org_id,run_id,invocation_id,receipt_id,actual_units,result_digest,outcome) "
                "VALUES($1,$2,$3,'lock-wait-receipt',0,$4,'not_executed')",
                worker.org_id,
                run.run_id,
                request.invocation_id,
                "f" * 64,
            )
        except BaseException:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            raise
    with pytest.raises(CloudConflict, match="final receipt"):
        await sender
    assert await pgcase.admin.fetchval("SELECT state FROM fh_cloud.invocations") == "admitted"


@pytest.mark.parametrize("operation", ["admit", "mark_sent", "mark_unknown", "settle"])
async def test_lease_expiry_during_row_lock_wait_rolls_back_all_state(pgcase, operation):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    if operation != "admit":
        await calls.admit(worker, ticket, request, grant)
    if operation in {"mark_unknown", "settle"}:
        await calls.mark_sent(worker, ticket, request.invocation_id)
    if operation == "settle":
        await calls.record_receipt(
            worker,
            run.run_id,
            request.invocation_id,
            Receipt(receipt_id="expiry-fact", actual_units=3, result_digest="f" * 64),
        )
    ticket = await pgcase.store.renew(worker, ticket, ttl_seconds=0.5)
    table = "approvals" if operation in {"admit", "mark_sent"} else "invocations"
    async with pgcase.admin.transaction():
        await pgcase.admin.execute(f"SELECT 1 FROM fh_cloud.{table} FOR UPDATE")
        args = (
            (worker, ticket, request, grant)
            if operation == "admit"
            else (
                worker,
                ticket,
                request.invocation_id,
            )
        )
        pending = asyncio.create_task(getattr(calls, operation)(*args))
        try:
            async with asyncio.timeout(5):
                await database_condition(
                    pgcase.admin,
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=$1 "
                    "AND wait_event_type='Lock')",
                    pgcase.role,
                )
                await database_condition(
                    pgcase.admin,
                    "SELECT clock_timestamp()>$1",
                    ticket.expires_at,
                )
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise
    with pytest.raises(StaleLease):
        await pending
    current = await pgcase.store.get(owner, run.run_id)
    assert current.used_units == 0
    if operation == "admit":
        assert current.reserved_units == 0
        assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0
        assert not await pgcase.admin.fetchval("SELECT consumed FROM fh_cloud.approvals")
    else:
        assert current.reserved_units == 4
        state = await pgcase.admin.fetchval("SELECT state FROM fh_cloud.invocations")
        assert state == ("admitted" if operation == "mark_sent" else "sent")


@pytest.mark.parametrize("operation", ["admit", "mark_sent"])
async def test_deadline_during_approval_lock_wait_prevents_new_call_or_send(pgcase, operation):
    deadline = datetime.now(UTC) + timedelta(seconds=2)
    calls, owner, worker, approver, run, ticket = await prepared(pgcase, deadline_at=deadline)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    if operation == "mark_sent":
        await calls.admit(worker, ticket, request, grant)
    async with pgcase.admin.transaction():
        await pgcase.admin.execute("SELECT 1 FROM fh_cloud.approvals FOR UPDATE")
        args = (
            (worker, ticket, request, grant)
            if operation == "admit"
            else (
                worker,
                ticket,
                request.invocation_id,
            )
        )
        pending = asyncio.create_task(getattr(calls, operation)(*args))
        try:
            async with asyncio.timeout(5):
                await database_condition(
                    pgcase.admin,
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=$1 "
                    "AND wait_event_type='Lock')",
                    pgcase.role,
                )
                await database_condition(pgcase.admin, "SELECT clock_timestamp()>$1", deadline)
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise
    with pytest.raises(CloudConflict, match="deadline"):
        await pending
    current = await pgcase.store.get(owner, run.run_id)
    assert current.reserved_units == (0 if operation == "admit" else 4)
    assert current.used_units == 0
    assert not any(e["kind"] == "call.sent" for e in await pgcase.store.events(owner, run.run_id))


@pytest.mark.parametrize("actual_units", [7, 12])
async def test_overspend_blocks_a_previously_admitted_unsent_call(pgcase, actual_units):
    calls, owner, worker, approver, run, ticket = await prepared(pgcase, max_units=10)
    first, second = call("spent"), call("not-yet-sent")
    for request in (first, second):
        grant = await calls.grant(approver, run.run_id, request)
        await calls.admit(worker, ticket, request, grant)
    await calls.mark_sent(worker, ticket, first.invocation_id)
    await calls.record_receipt(
        worker,
        run.run_id,
        first.invocation_id,
        Receipt(receipt_id="actual-overspend", actual_units=actual_units, result_digest="f" * 64),
    )
    await calls.settle(worker, ticket, first.invocation_id)
    with pytest.raises(CloudConflict, match="budget"):
        await calls.mark_sent(worker, ticket, second.invocation_id)
    current = await pgcase.store.get(owner, run.run_id)
    assert current.used_units == actual_units and current.reserved_units == 4
    events = await pgcase.store.events(owner, run.run_id)
    assert sum(e["kind"] == "call.sent" for e in events) == 1
    await calls.record_receipt(
        worker,
        run.run_id,
        second.invocation_id,
        Receipt(
            receipt_id="verified-not-sent",
            actual_units=0,
            result_digest="e" * 64,
            outcome="not_executed",
        ),
    )
    await calls.settle(worker, ticket, second.invocation_id)
    finished = await pgcase.store.finish(worker, ticket, "exhausted")
    assert finished.used_units == actual_units and finished.reserved_units == 0


async def test_actual_receipt_ingestion_and_send_follow_the_same_lock_order(pgcase):
    calls, _, worker, approver, run, ticket = await prepared(pgcase)
    request = call()
    grant = await calls.grant(approver, run.run_id, request)
    await calls.admit(worker, ticket, request, grant)
    pending = []
    async with pgcase.admin.transaction():
        await pgcase.admin.execute("SELECT 1 FROM fh_cloud.invocations FOR UPDATE")
        receipt = Receipt(
            receipt_id="real-api-race",
            actual_units=0,
            result_digest="f" * 64,
            outcome="not_executed",
        )
        pending.append(
            asyncio.create_task(
                calls.record_receipt(
                    worker,
                    run.run_id,
                    request.invocation_id,
                    receipt,
                )
            )
        )
        try:
            await database_condition(
                pgcase.admin,
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=$1 "
                "AND wait_event_type='Lock' AND query LIKE '%fh_cloud.invocations%')",
                pgcase.role,
            )
            pending.append(
                asyncio.create_task(calls.mark_sent(worker, ticket, request.invocation_id))
            )
            await database_condition(
                pgcase.admin,
                "SELECT COUNT(*)>=2 FROM pg_stat_activity WHERE usename=$1 "
                "AND wait_event_type='Lock'",
                pgcase.role,
            )
        except BaseException:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
    async with asyncio.timeout(5):
        recorded, sending = await asyncio.gather(*pending, return_exceptions=True)
    assert recorded is None
    assert isinstance(sending, CloudConflict)
    assert await pgcase.admin.fetchval("SELECT state FROM fh_cloud.invocations") == "admitted"
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.receipts") == 1

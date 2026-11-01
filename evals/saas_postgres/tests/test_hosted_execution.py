"""Actual hosted execution acceptance and independent business-oracle checks."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from forgeharness.cloud.models import CloudConflict, CloudDenied
from forgeharness.domain.models import Actor, ResourceScope, RunContext, ToolCall
from forgeharness.tools.base import ToolContext

from .execution_fixtures import (
    SyntheticBusinessLedger,
    SyntheticResponseLost,
    application_fixture,
    hosted_registration,
)


def owner_context(domain="orders", run_id="run-1"):
    return RunContext(
        application_id="synthetic-" + domain,
        application_version="1",
        policy_version="1",
        run_id=run_id,
        session_id="synthetic-session",
        correlation_id="synthetic-correlation",
        actor=Actor(id="owner", roles=("user",)),
        scope=ResourceScope(tenant_id="org-a", resources=("record:record-1",)),
    )


def tool_context(domain="orders", *, key="physical-1", logical_id="write-record"):
    return ToolContext(
        task_id="run-1",
        run_context=owner_context(domain),
        attempt_id=logical_id + "#1",
        logical_call_id=logical_id,
        idempotency_key=key,
    )


async def test_business_oracle_honors_the_actual_context_idempotency_key(tmp_path):
    """Fixture self-check; this alone is not a PostgreSQL execution qualification."""
    ledger = SyntheticBusinessLedger(tmp_path / "external-business.sqlite")
    ledger.seed("orders")
    fixture = application_fixture(ledger)
    tool = fixture.application.tools[1]
    arguments = tool.input_model(record_key="record-1", value="approved")
    context = tool_context()
    first = await tool.execute(arguments, context)
    repeated = await tool.execute(arguments, context)
    assert repeated == first
    assert ledger.effect_count("orders") == 1
    assert ledger.receipt("physical-1").dispatches == 2
    assert ledger.record("orders")["revision"] == 2
    call = ToolCall(id="write-record", name="oracle.write", arguments=arguments.model_dump())
    assert (
        ledger.verify_tool("orders", call, context, first).receipt_id == first.receipt.resource_id
    )
    with pytest.raises(ValueError, match="changed arguments"):
        await tool.execute(tool.input_model(record_key="record-1", value="changed"), context)
    assert ledger.effect_count("orders") == 1
    with pytest.raises(ValueError, match="independently verified"):
        ledger.verify_tool("orders", call, context, first.model_copy(update={"content": "forged"}))


async def test_lost_business_response_leaves_an_independent_authoritative_receipt(tmp_path):
    ledger = SyntheticBusinessLedger(tmp_path / "external-business.sqlite")
    ledger.seed("orders")
    fixture = application_fixture(ledger)
    fixture.faults.arm("write.after_commit", raise_after=True)
    tool = fixture.application.tools[1]
    with pytest.raises(SyntheticResponseLost):
        await tool.execute(
            tool.input_model(record_key="record-1", value="approved"), tool_context()
        )
    assert ledger.effect_count("orders") == 1
    assert ledger.receipt("physical-1").dispatches == 1
    assert ledger.record("orders")["value"] == "approved"


async def test_distinct_applications_share_tool_contract_without_sharing_business_effects(tmp_path):
    ledger = SyntheticBusinessLedger(tmp_path / "external-business.sqlite")
    for domain in ("orders", "cases"):
        ledger.seed(domain)
        fixture = application_fixture(ledger, domain)
        tool = fixture.application.tools[1]
        assert tool.spec.name == "oracle.write"
        await tool.execute(
            tool.input_model(record_key="record-1", value="approved"),
            tool_context(domain, key="physical-" + domain),
        )
    assert ledger.effect_count("orders") == ledger.effect_count("cases") == 1
    assert len(ledger.all_receipts("tool")) == 2


def physical_write(registration):
    from forgeharness.cloud.execution_contract import CallEnvelope
    from forgeharness.cloud.hosted import PhysicalCall
    from forgeharness.domain.models import Usage

    call = ToolCall(
        id="write-record",
        name="oracle.write",
        arguments={"record_key": "record-1", "value": "approved"},
    )
    envelope = CallEnvelope(
        kind="tool",
        logical_id=call.id,
        canonical_input={
            "call": call.model_dump(mode="json"),
            "tool_spec": registration.application.tools[1].spec.model_dump(mode="json"),
        },
        reservation=Usage(tool_calls=1),
        reserve_units=10,
    )
    return PhysicalCall(
        owner=owner_context(),
        envelope=envelope,
        idempotency_key=envelope.idempotency_key("org-a", "run-1", registration.binding_digest),
    )


async def test_registered_oracle_verifier_freezes_actual_dispatch_for_exact_resolution(tmp_path):
    from forgeharness.tools.dispatcher import DispatchResult

    ledger = SyntheticBusinessLedger(tmp_path / "external-business.sqlite")
    ledger.seed("orders")
    fixture = application_fixture(ledger)
    registration = hosted_registration(fixture)
    physical = physical_write(registration)
    tool = fixture.application.tools[1]
    output = await tool.execute(
        tool.input_model(record_key="record-1", value="approved"),
        tool_context(key=physical.idempotency_key),
    )
    confirmed = await registration.tool_verifier(
        physical, DispatchResult(output=output, elapsed_ms=7)
    )
    assert confirmed.actual_units == 3
    assert confirmed.actual_usage.tool_calls == 1
    resolved = await registration.resolver(physical)
    assert resolved == confirmed
    assert resolved.tool_result.elapsed_ms == 7
    forged = DispatchResult(output=output.model_copy(update={"content": "forged"}), elapsed_ms=7)
    assert await registration.tool_verifier(physical, forged) is None
    assert ledger.effect_count("orders") == 1
    assert ledger.receipt(physical.idempotency_key).dispatches == 1


async def test_registered_resolver_confirms_a_lost_response_without_dispatching_again(tmp_path):
    from forgeharness.tools.dispatcher import DispatchResult

    ledger = SyntheticBusinessLedger(tmp_path / "external-business.sqlite")
    ledger.seed("orders")
    fixture = application_fixture(ledger)
    registration = hosted_registration(fixture)
    physical = physical_write(registration)
    fixture.faults.arm("write.after_commit", raise_after=True)
    tool = fixture.application.tools[1]
    with pytest.raises(SyntheticResponseLost):
        await tool.execute(
            tool.input_model(record_key="record-1", value="approved"),
            tool_context(key=physical.idempotency_key),
        )
    resolved = await registration.resolver(physical)
    assert resolved.actual_units == 3
    assert resolved.tool_result.output.ok
    assert await registration.resolver(physical) == resolved
    # A different late physical response cannot silently rewrite the frozen fact.
    late = DispatchResult(output=resolved.tool_result.output, elapsed_ms=9)
    assert await registration.tool_verifier(physical, late) is None
    assert ledger.effect_count("orders") == 1
    assert ledger.receipt(physical.idempotency_key).dispatches == 1


async def hosted_case(pgcase, tmp_path, *, domain="orders", invalid_final=False):
    from forgeharness.cloud.runtime import HostedRuntime

    ledger = SyntheticBusinessLedger(tmp_path / "external-business.sqlite")
    ledger.seed(domain)
    fixture = application_fixture(ledger, domain, invalid_final=invalid_final)
    registration = hosted_registration(fixture)
    runtime = HostedRuntime(pgcase.store, {registration.deployment_id: registration})
    owner = await pgcase.seed("org-a", "owner", can_execute=False)
    reviewer = await pgcase.seed("org-a", "reviewer", can_execute=False, can_approve=True)
    worker = await pgcase.seed("org-a", "worker", can_execute=True)
    return runtime, fixture, registration, owner, reviewer, worker


async def independent_advance(pgcase, fixture, worker, run_id):
    env = dict(os.environ)
    env["FH_TEST_PG_HOSTED_RUNTIME_DSN"] = pgcase.runtime_dsn
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "evals.saas_postgres.tests.execution_fixtures",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    payload = json.dumps(
        {
            "ledger_path": str(fixture.ledger.path),
            "domain": fixture.domain,
            "worker": worker.model_dump(mode="json"),
            "run_id": run_id,
        }
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(payload.encode()), timeout=30)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    # The child's controlled JSON contains state only; never echo arbitrary stderr.
    result = json.loads(stdout)
    assert process.returncode == 0, result.get("error_type", "independent hosted worker failed")
    return result


@pytest.mark.parametrize("domain", ["orders", "cases"])
async def test_real_pg_model_read_independent_approval_write_final_and_query(
    pgcase, tmp_path, domain
):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(
        pgcase, tmp_path, domain=domain
    )
    accepted = await runtime.accept(
        owner,
        registration.deployment_id,
        "business-request",
        "Read record-1 and update it to approved after approval.",
    )
    run_id = accepted.run.run_id
    duplicate = await runtime.accept(
        owner,
        registration.deployment_id,
        "business-request",
        "Read record-1 and update it to approved after approval.",
    )
    assert duplicate.run.run_id == run_id
    with pytest.raises(CloudConflict):
        await runtime.accept(owner, registration.deployment_id, "business-request", "changed input")
    pending = await runtime.advance(worker, run_id, worker_id="worker-first")
    assert pending.run.status == pending.checkpoint.status == "awaiting_approval"
    assert pending.checkpoint.pending_approval.call.name == "oracle.write"
    assert fixture.ledger.effect_count(domain) == 0
    assert len(fixture.ledger.all_receipts("model")) == 2
    assert len(fixture.ledger.all_receipts("tool")) == 1
    assert fixture.verifier.calls == 0
    with pytest.raises(CloudDenied):
        await runtime.approve(owner, run_id)
    await runtime.approve(reviewer, run_id)
    finished = await runtime.advance(worker, run_id, worker_id="worker-second")
    assert finished.run.status == finished.checkpoint.status == "succeeded"
    assert fixture.verifier.calls == 1
    assert fixture.ledger.effect_count(domain) == 1
    assert fixture.ledger.record(domain)["value"] == "approved"
    assert finished.run.used_units == 20
    assert finished.run.reserved_units == 0
    assert finished.checkpoint.usage.tool_calls == 2
    assert finished.checkpoint.usage.steps == 3
    assert len(fixture.ledger.all_receipts()) == 5
    assert all(item.dispatches == 1 for item in fixture.ledger.all_receipts())
    inspected = await runtime.inspect(owner, run_id)
    assert inspected.checkpoint.final_output == finished.checkpoint.final_output
    assert json.loads(inspected.checkpoint.final_output)["domain"] == domain
    trace = await runtime.trace(owner, run_id)
    assert {event.type for event in trace.events} >= {
        "model.request",
        "model.action",
        "tool.completed",
    }
    assert trace.snapshot_pending() == ()
    # No SQLite checkpoint/approval/journal was created by hosted execution.
    assert tuple(tmp_path.glob("*.sqlite")) == (fixture.ledger.path,)


async def test_real_pg_unknown_write_recovers_in_a_fresh_process_without_redispatch(
    pgcase, tmp_path
):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "lost-response", "approve update"
    )
    run_id = accepted.run.run_id
    await runtime.advance(worker, run_id, worker_id="first")
    await runtime.approve(reviewer, run_id)
    fixture.faults.arm("write.after_commit", raise_after=True)
    unknown = await runtime.advance(worker, run_id, worker_id="lost-response-worker", ttl_seconds=2)
    assert unknown.run.status == "reconciling"
    assert unknown.run.reserved_units > 0
    assert fixture.ledger.effect_count("orders") == 1
    second_worker = await pgcase.seed("org-a", "second-worker", can_execute=True)
    await asyncio.sleep(2.05)
    recovered = await independent_advance(pgcase, fixture, second_worker, run_id)
    assert recovered["status"] == recovered["checkpoint_status"] == "succeeded"
    assert recovered["epoch"] > unknown.run.epoch
    assert recovered["used_units"] == 20
    assert recovered["reserved_units"] == 0
    assert fixture.ledger.effect_count("orders") == 1
    assert all(item.dispatches == 1 for item in fixture.ledger.all_receipts())
    assert (await runtime.inspect(owner, run_id)).checkpoint.status == "succeeded"


async def test_real_pg_paused_old_worker_cannot_overwrite_independent_takeover(pgcase, tmp_path):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "paused-worker", "approve update"
    )
    run_id = accepted.run.run_id
    await runtime.advance(worker, run_id, worker_id="first")
    await runtime.approve(reviewer, run_id)
    fixture.faults.arm("write.after_commit")
    old_worker = asyncio.create_task(
        runtime.advance(worker, run_id, worker_id="paused", ttl_seconds=2)
    )
    try:
        await asyncio.wait_for(fixture.faults.entered.wait(), timeout=5)
        assert fixture.ledger.effect_count("orders") == 1
        second_worker = await pgcase.seed("org-a", "second-worker", can_execute=True)
        await asyncio.sleep(2.05)
        recovered = await independent_advance(pgcase, fixture, second_worker, run_id)
        assert recovered["status"] == "succeeded"
        fixture.faults.release.set()
        # An old worker may receive StaleLease or an already-final view, but it
        # must not replace the new worker's projection or replay a business write.
        await asyncio.wait_for(asyncio.gather(old_worker, return_exceptions=True), timeout=5)
        final = await runtime.inspect(owner, run_id)
        assert final.run.status == "succeeded"
        assert final.run.epoch == recovered["epoch"]
        assert final.run.used_units == 20
        assert final.run.reserved_units == 0
        assert fixture.ledger.effect_count("orders") == 1
        assert all(item.dispatches == 1 for item in fixture.ledger.all_receipts())
    finally:
        fixture.faults.release.set()
        await asyncio.gather(old_worker, return_exceptions=True)


async def test_real_pg_completed_effect_does_not_bypass_the_application_verifier(pgcase, tmp_path):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(
        pgcase, tmp_path, invalid_final=True
    )
    accepted = await runtime.accept(
        owner, registration.deployment_id, "invalid-final", "approve update"
    )
    run_id = accepted.run.run_id
    await runtime.advance(worker, run_id, worker_id="first")
    await runtime.approve(reviewer, run_id)
    final = await runtime.advance(worker, run_id, worker_id="second")
    assert final.run.status in {"failed", "exhausted"}
    assert final.checkpoint.status in {"failed", "exhausted"}
    assert fixture.verifier.calls >= 1
    assert fixture.ledger.effect_count("orders") == 1
    assert final.run.used_units >= 20
    assert final.run.reserved_units == 0
    assert final.checkpoint.final_output is None


async def test_real_pg_query_uses_current_membership_and_hides_foreign_execution(pgcase, tmp_path):
    runtime, fixture, registration, owner, _reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "private-run", "approve update"
    )
    run_id = accepted.run.run_id
    await runtime.advance(worker, run_id, worker_id="first")
    foreign = await pgcase.seed("org-b", "foreign", can_execute=False)
    for unavailable in (run_id, "missing-run"):
        with pytest.raises(CloudDenied):
            await runtime.inspect(foreign, unavailable)
        with pytest.raises(CloudDenied):
            await runtime.trace(foreign, unavailable)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.members SET active=false WHERE org_id=$1 AND actor_id=$2",
        owner.org_id,
        owner.actor_id,
    )
    with pytest.raises(CloudDenied):
        await runtime.inspect(owner, run_id)
    assert fixture.ledger.effect_count("orders") == 0


async def test_real_pg_cancel_pending_action_preserves_prior_cost_without_a_business_write(
    pgcase, tmp_path
):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "cancel-pending", "approve update"
    )
    run_id = accepted.run.run_id
    await runtime.advance(worker, run_id, worker_id="first")
    await runtime.cancel(owner, run_id)
    with pytest.raises((CloudDenied, CloudConflict)):
        await runtime.approve(reviewer, run_id)
    cancelled = await runtime.advance(worker, run_id, worker_id="cancel-worker")
    assert cancelled.run.status == cancelled.checkpoint.status == "cancelled"
    assert cancelled.run.used_units == 12
    assert cancelled.run.reserved_units == 0
    assert fixture.ledger.effect_count("orders") == 0
    assert len(fixture.ledger.all_receipts()) == 3


async def test_real_pg_revoked_owner_cannot_execute_a_previously_approved_write(pgcase, tmp_path):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "revoke-owner", "approve update"
    )
    run_id = accepted.run.run_id
    await runtime.advance(worker, run_id, worker_id="first")
    await runtime.approve(reviewer, run_id)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.members SET active=false WHERE org_id=$1 AND actor_id=$2",
        owner.org_id,
        owner.actor_id,
    )
    try:
        stopped = await runtime.advance(worker, run_id, worker_id="post-revocation")
        assert stopped.run.status != "succeeded"
    except CloudDenied:
        pass
    assert fixture.ledger.effect_count("orders") == 0
    assert len(fixture.ledger.all_receipts()) == 3


async def test_real_pg_advance_requires_executor_even_before_work_and_after_completion(
    pgcase, tmp_path
):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "executor-only", "approve update"
    )
    run_id = accepted.run.run_id
    with pytest.raises(CloudDenied):
        await runtime.advance(owner, run_id)
    assert fixture.ledger.all_receipts() == ()
    pending = await runtime.advance(worker, run_id)
    assert pending.checkpoint.status == "awaiting_approval"
    with pytest.raises(CloudDenied):
        await runtime.advance(owner, run_id)
    await runtime.approve(reviewer, run_id)
    assert (await runtime.advance(worker, run_id)).checkpoint.status == "succeeded"
    with pytest.raises(CloudDenied):
        await runtime.advance(owner, run_id)
    assert all(item.dispatches == 1 for item in fixture.ledger.all_receipts())


async def test_real_pg_approval_hides_missing_and_foreign_runs(pgcase, tmp_path):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "private-approval", "approve update"
    )
    await runtime.advance(worker, accepted.run.run_id)
    foreign = await pgcase.seed("org-b", "foreign-reviewer", can_execute=False, can_approve=True)
    for actor, run_id in (
        (reviewer, "missing-run"),
        (foreign, accepted.run.run_id),
        (foreign, "missing-run"),
    ):
        with pytest.raises(CloudDenied):
            await runtime.approve(actor, run_id)
    assert fixture.ledger.effect_count("orders") == 0
    assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.approvals") == 0


async def test_real_pg_waiting_without_approval_stops_when_owner_is_revoked(pgcase, tmp_path):
    runtime, fixture, registration, owner, _reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "revoked-waiting", "approve update"
    )
    run_id = accepted.run.run_id
    assert (await runtime.advance(worker, run_id)).checkpoint.status == "awaiting_approval"
    await pgcase.admin.execute(
        "UPDATE fh_cloud.members SET active=false WHERE org_id=$1 AND actor_id=$2",
        owner.org_id,
        owner.actor_id,
    )
    stopped = await runtime.advance(worker, run_id)
    assert stopped.run.status == stopped.checkpoint.status == "failed"
    assert stopped.checkpoint.pending_approval is None
    assert stopped.run.used_units == 12
    assert stopped.run.reserved_units == 0
    assert fixture.ledger.effect_count("orders") == 0
    assert len(fixture.ledger.all_receipts()) == 3
    with pytest.raises(CloudDenied):
        await runtime.inspect(owner, run_id)


async def test_real_pg_suspended_org_reconciles_committed_write_then_stops(pgcase, tmp_path):
    runtime, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "suspended-org", "approve update"
    )
    run_id = accepted.run.run_id
    await runtime.advance(worker, run_id)
    await runtime.approve(reviewer, run_id)
    fixture.faults.arm("write.after_commit", raise_after=True)
    unknown = await runtime.advance(worker, run_id)
    assert unknown.run.status == "reconciling"
    assert unknown.run.reserved_units > 0
    assert fixture.ledger.effect_count("orders") == 1
    await pgcase.admin.execute(
        "UPDATE fh_cloud.organizations SET active=false WHERE org_id=$1", owner.org_id
    )
    for operation in (runtime.inspect, runtime.trace):
        with pytest.raises(CloudDenied):
            await operation(owner, run_id)
    stopped = await runtime.advance(worker, run_id, worker_id="reconcile-suspended-org")
    assert stopped.run.status == stopped.checkpoint.status == "failed"
    assert stopped.run.used_units == 15
    assert stopped.run.reserved_units == 0
    assert stopped.checkpoint.usage.steps == 2
    assert stopped.checkpoint.usage.tool_calls == 2
    assert stopped.checkpoint.final_output is None
    assert fixture.ledger.effect_count("orders") == 1
    assert len(fixture.ledger.all_receipts()) == 4
    assert all(item.dispatches == 1 for item in fixture.ledger.all_receipts())


async def test_real_pg_busy_worker_cannot_dispatch_the_claimed_call_again(pgcase, tmp_path):
    runtime, fixture, registration, owner, _reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "parallel-worker", "approve update"
    )
    run_id = accepted.run.run_id
    second_worker = await pgcase.seed("org-a", "second-worker", can_execute=True)
    fixture.faults.arm("read.before_effect")
    first = asyncio.create_task(runtime.advance(worker, run_id, worker_id="first", ttl_seconds=30))
    try:
        await asyncio.wait_for(fixture.faults.entered.wait(), timeout=5)
        with pytest.raises(CloudConflict):
            await asyncio.wait_for(
                runtime.advance(second_worker, run_id, worker_id="second"), timeout=5
            )
        assert fixture.ledger.effect_count("orders") == 0
        assert len(fixture.ledger.all_receipts()) == 1
        fixture.faults.release.set()
        pending = await asyncio.wait_for(first, timeout=5)
        assert pending.checkpoint.status == "awaiting_approval"
        assert len(fixture.ledger.all_receipts()) == 3
        assert all(item.dispatches == 1 for item in fixture.ledger.all_receipts())
    finally:
        fixture.faults.release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), 301])
async def test_real_pg_invalid_worker_ttl_cannot_start_a_physical_call(pgcase, tmp_path, ttl):
    runtime, fixture, registration, owner, _reviewer, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "invalid-ttl", "approve update"
    )
    with pytest.raises(ValueError, match="lease ttl"):
        await runtime.advance(worker, accepted.run.run_id, ttl_seconds=ttl)
    unchanged = await runtime.inspect(owner, accepted.run.run_id)
    assert unchanged.run.epoch == 0
    assert unchanged.run.used_units == unchanged.run.reserved_units == 0
    assert unchanged.checkpoint.status == "created"
    assert fixture.ledger.all_receipts() == ()

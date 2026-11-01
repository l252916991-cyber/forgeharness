"""Budget exhaustion preserves settled facts and discards uncommitted intents."""

import asyncio
from dataclasses import replace

from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.domain.models import FinalAction, ModelResult, ModelUsage, RunStatus, Usage
from forgeharness.runtime.verification import VerificationResult

from .execution_fixtures import (
    SyntheticBusinessLedger,
    application_fixture,
    digest,
    hosted_registration,
)


async def budget_fixture(pgcase, tmp_path, *, max_units, model_reservation=1_000):
    ledger = SyntheticBusinessLedger(tmp_path / "independent-budget-provider.sqlite")
    ledger.seed("orders")
    fixture = application_fixture(ledger)
    registration = replace(
        hosted_registration(fixture),
        max_run_units=max_units,
        model_reservation_units=model_reservation,
    )
    runtime = HostedRuntime(pgcase.store, {registration.deployment_id: registration})
    owner = await pgcase.seed("org-a", "owner", can_execute=False)
    worker = await pgcase.seed("org-a", "worker")
    return runtime, fixture, registration, owner, worker


async def test_rejected_first_model_admission_does_not_leave_a_fake_admitted_trace(
    pgcase, tmp_path
):
    runtime, fixture, registration, owner, worker = await budget_fixture(
        pgcase, tmp_path, max_units=0
    )
    accepted = await runtime.accept(
        owner, registration.deployment_id, "no-model-budget", "Read record-1."
    )

    exhausted = await runtime.advance(worker, accepted.run.run_id)

    assert exhausted.run.status == "exhausted"
    assert exhausted.checkpoint.status is RunStatus.EXHAUSTED
    assert exhausted.checkpoint.usage == Usage()
    assert exhausted.run.used_units == exhausted.run.reserved_units == 0
    assert not fixture.ledger.all_receipts()
    assert fixture.ledger.effect_count("orders") == 0
    for table in ("invocations", "receipts", "execution_calls", "execution_logical_usage"):
        assert await pgcase.admin.fetchval(f"SELECT COUNT(*) FROM fh_cloud.{table}") == 0
    trace = await runtime.trace(owner, accepted.run.run_id)
    assert sum(event.type == "execution.budget_exhausted" for event in trace.events) == 1
    assert not any(
        event.type in {"execution.call.admitted", "execution.call.sending", "approval.consumed"}
        for event in trace.events
    )
    assert not any(
        event["kind"] == "execution.call_admitted"
        for event in await pgcase.store.events(owner, accepted.run.run_id)
    )
    assert await runtime.advance(worker, accepted.run.run_id) == exhausted
    assert not fixture.ledger.all_receipts()


class FinalResponseModel:
    def __init__(self, ledger):
        self.ledger = ledger

    async def decide(self, request):
        result = ModelResult(
            action=FinalAction(content="No business changes requested."),
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            model_name="synthetic-final-provider-v1",
        )
        return await asyncio.to_thread(self.ledger.model, request, result)


class UnchangedBusinessVerifier:
    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = 0

    async def verify(self, request):
        self.calls += 1
        receipts = await asyncio.to_thread(self.ledger.all_receipts, "model")
        record = await asyncio.to_thread(self.ledger.record, "orders")
        effects = await asyncio.to_thread(self.ledger.effect_count, "orders")
        passed = (
            len(receipts) == 1
            and receipts[0].physical_fact is not None
            and receipts[0].request["task_id"] == request.task_id
            and receipts[0].result["action"] == request.final.model_dump(mode="json")
            and request.run_context is not None
            and request.run_context.scope.tenant_id == "org-a"
            and record["value"] == "draft"
            and record["revision"] == 1
            and effects == 0
            and not request.tool_results
        )
        return VerificationResult(
            passed=passed,
            reason="independent provider receipt and unchanged business state verified",
            evidence=("synthetic-ledger://unchanged",) if passed else (),
        )


async def assert_exhausted_without_committed_success(runtime, owner, worker, exhausted):
    assert exhausted.run.status == "exhausted"
    assert exhausted.checkpoint.status is RunStatus.EXHAUSTED
    assert exhausted.checkpoint.final_output is None
    assert exhausted.checkpoint.pending_approval is None
    assert exhausted.checkpoint.authorized_call_id is None
    assert exhausted.run.reserved_units == 0
    trace = await runtime.trace(owner, exhausted.run.run_id)
    assert sum(event.type == "execution.budget_exhausted" for event in trace.events) == 1
    assert not any(
        event.type in {"checkpoint.saved", "run.finished"}
        and event.payload.get("status") == "succeeded"
        for event in trace.events
    )
    assert not any(
        event["kind"] == "execution.checkpoint" and event["payload"].get("status") == "succeeded"
        for event in await runtime.store.events(owner, exhausted.run.run_id)
    )
    assert await runtime.advance(worker, exhausted.run.run_id) == exhausted


async def test_actual_final_model_cost_over_cap_settles_and_converges_to_exhausted(
    pgcase, tmp_path
):
    _, fixture, registration, owner, worker = await budget_fixture(
        pgcase, tmp_path, max_units=1, model_reservation=1
    )
    verifier = UnchangedBusinessVerifier(fixture.ledger)
    registration = replace(
        registration,
        application=replace(
            fixture.application,
            model_factory=lambda: FinalResponseModel(fixture.ledger),
            verifier=verifier,
        ),
        release_digest=digest({"test": "actual-final-model-overspend-v1"}),
        model_binding_digest=digest({"model": "synthetic-final-provider-v1"}),
    )
    runtime = HostedRuntime(pgcase.store, {registration.deployment_id: registration})
    accepted = await runtime.accept(
        owner, registration.deployment_id, "actual-overspend", "Confirm no business changes."
    )

    exhausted = await runtime.advance(worker, accepted.run.run_id)

    assert exhausted.run.used_units == 5
    assert exhausted.checkpoint.usage == Usage(steps=1, input_tokens=3, output_tokens=2)
    assert verifier.calls == 1
    provider = fixture.ledger.all_receipts("model")
    assert len(provider) == 1 and provider[0].actual_units == 5 and provider[0].dispatches == 1
    assert not fixture.ledger.all_receipts("tool")
    fact = await pgcase.admin.fetchrow(
        "SELECT i.state,c.kind,c.canonical_output,c.actual_usage,r.actual_units,r.outcome "
        "FROM fh_cloud.invocations i JOIN fh_cloud.execution_calls c "
        "USING(org_id,run_id,invocation_id) JOIN fh_cloud.receipts r "
        "USING(org_id,run_id,invocation_id)"
    )
    assert fact["state"] == "settled" and fact["kind"] == "model"
    assert fact["actual_units"] == 5 and fact["outcome"] == "completed"
    assert fact["canonical_output"] is not None and fact["actual_usage"] is not None
    await assert_exhausted_without_committed_success(runtime, owner, worker, exhausted)
    assert len(fixture.ledger.all_receipts("model")) == 1
    assert fixture.ledger.all_receipts("model")[0].dispatches == 1


async def test_actual_last_model_overspend_preserves_approved_write_and_all_costs(pgcase, tmp_path):
    _, fixture, registration, owner, worker = await budget_fixture(
        pgcase, tmp_path, max_units=18, model_reservation=1
    )
    registration = replace(
        registration, tool_reservation_units={"oracle.read": 2, "oracle.write": 3}
    )
    runtime = HostedRuntime(pgcase.store, {registration.deployment_id: registration})
    reviewer = await pgcase.seed("org-a", "reviewer", can_execute=False, can_approve=True)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "actual-final-overspend", "Approve record-1."
    )
    pending = await runtime.advance(worker, accepted.run.run_id)
    assert pending.checkpoint.status is RunStatus.AWAITING_APPROVAL
    assert pending.run.used_units == 12
    grant = await runtime.approve(reviewer, accepted.run.run_id)

    exhausted = await runtime.advance(worker, accepted.run.run_id)

    assert exhausted.run.used_units == 20
    assert exhausted.checkpoint.usage == Usage(
        steps=3, tool_calls=2, input_tokens=9, output_tokens=6
    )
    assert fixture.verifier.calls == 1
    assert fixture.ledger.effect_count("orders") == 1
    assert fixture.ledger.record("orders")["value"] == "approved"
    assert len(fixture.ledger.all_receipts()) == 5
    assert all(receipt.dispatches == 1 for receipt in fixture.ledger.all_receipts())
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.receipts") == 5
    assert await pgcase.admin.fetchval("SELECT SUM(actual_units) FROM fh_cloud.receipts") == 20
    assert (
        await pgcase.admin.fetchval(
            "SELECT COUNT(*) FROM fh_cloud.invocations WHERE state='settled'"
        )
        == 5
    )
    assert await pgcase.admin.fetchval(
        "SELECT consumed FROM fh_cloud.approvals WHERE approval_id=$1", grant.id
    )
    await assert_exhausted_without_committed_success(runtime, owner, worker, exhausted)
    assert fixture.ledger.effect_count("orders") == 1
    assert all(receipt.dispatches == 1 for receipt in fixture.ledger.all_receipts())


async def test_rejected_approved_tool_admission_does_not_consume_grant_or_commit_intents(
    pgcase, tmp_path
):
    runtime, fixture, registration, owner, worker = await budget_fixture(
        pgcase, tmp_path, max_units=20, model_reservation=6
    )
    reviewer = await pgcase.seed("org-a", "reviewer", can_execute=False, can_approve=True)
    accepted = await runtime.accept(
        owner, registration.deployment_id, "insufficient-approved-write", "Approve record-1."
    )
    pending = await runtime.advance(worker, accepted.run.run_id)
    assert pending.checkpoint.status is RunStatus.AWAITING_APPROVAL
    assert pending.run.used_units == 12
    grant = await runtime.approve(reviewer, accepted.run.run_id)

    exhausted = await runtime.advance(worker, accepted.run.run_id)

    assert exhausted.run.status == "exhausted"
    assert exhausted.checkpoint.status is RunStatus.EXHAUSTED
    assert exhausted.run.used_units == 12 and exhausted.run.reserved_units == 0
    assert exhausted.checkpoint.usage == Usage(
        steps=2, tool_calls=1, input_tokens=6, output_tokens=4
    )
    assert exhausted.checkpoint.pending_approval is None
    assert exhausted.checkpoint.authorized_call_id is None
    assert exhausted.checkpoint.final_output is None
    assert not await pgcase.admin.fetchval(
        "SELECT consumed FROM fh_cloud.approvals WHERE approval_id=$1", grant.id
    )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 3
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.receipts") == 3
    assert (
        await pgcase.admin.fetchval(
            "SELECT COUNT(*) FROM fh_cloud.invocations WHERE tool_name='oracle.write'"
        )
        == 0
    )
    assert len(fixture.ledger.all_receipts("model")) == 2
    assert len(fixture.ledger.all_receipts("tool")) == 1
    assert all(item.dispatches == 1 for item in fixture.ledger.all_receipts())
    assert fixture.ledger.effect_count("orders") == 0 and fixture.verifier.calls == 0
    trace = await runtime.trace(owner, accepted.run.run_id)
    assert sum(event.type == "execution.call.admitted" for event in trace.events) == 3
    assert sum(event.type == "execution.budget_exhausted" for event in trace.events) == 1
    assert not any(event.type == "approval.consumed" for event in trace.events)
    next_revision = pending.checkpoint.checkpoint_revision + 1
    assert [
        event.payload["status"]
        for event in trace.events
        if event.type == "checkpoint.saved" and event.payload["revision"] == next_revision
    ] == ["exhausted"]
    assert (
        sum(
            event["kind"] == "execution.call_admitted"
            for event in await pgcase.store.events(owner, accepted.run.run_id)
        )
        == 3
    )
    assert await runtime.advance(worker, accepted.run.run_id) == exhausted

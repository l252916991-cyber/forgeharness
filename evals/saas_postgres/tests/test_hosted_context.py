"""Real-PG scoped context uses the persisted first transcript across approvals."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.context.provider import ContextMaterial
from forgeharness.sdk.spec import ScopeError

from .test_hosted_execution import hosted_case


class Materials:
    def __init__(self):
        self.calls = 0
        self.content = "version-one-reference"
        self.overrides = {}

    async def provide(self, *, task, context):
        self.calls += 1
        return (
            ContextMaterial(
                **{
                    "id": "record-reference",
                    "source": "synthetic-reference",
                    "content": self.content,
                    "version": "v1",
                    "tenant_id": context.scope.tenant_id,
                    "application_id": context.application_id,
                    "resource_id": "record:record-1",
                    **self.overrides,
                }
            ),
        )


async def with_materials(pgcase, tmp_path):
    _, fixture, registration, owner, reviewer, worker = await hosted_case(pgcase, tmp_path)
    provider = Materials()
    app = replace(registration.application, context_providers=(provider,))
    registered = replace(registration, application=app)
    runtime = HostedRuntime(pgcase.store, {registered.deployment_id: registered})
    return runtime, fixture, registered, provider, owner, reviewer, worker


async def test_context_is_scoped_versioned_persisted_and_not_refetched_on_resume(pgcase, tmp_path):
    runtime, fixture, app, provider, owner, reviewer, worker = await with_materials(
        pgcase, tmp_path
    )
    accepted = await runtime.accept(owner, app.deployment_id, "context-request", "approve update")
    run_id = accepted.run.run_id
    pending = await runtime.advance(worker, run_id)
    assert pending.checkpoint.status == "awaiting_approval"
    assert provider.calls == 1
    first = [m.content for m in pending.checkpoint.messages if m.role == "system"]
    assert any("untrusted reference data" in m and "version-one-reference" in m for m in first)
    provider.content = "version-two-reference"
    await runtime.approve(reviewer, run_id)
    finished = await runtime.advance(worker, run_id)
    assert finished.checkpoint.status == "succeeded"
    assert provider.calls == 1
    assert [m.content for m in finished.checkpoint.messages if m.role == "system"] == first
    trace = await runtime.trace(owner, run_id)
    compiled = [event for event in trace.events if event.type == "context.compiled"]
    assert len(compiled) == 1
    assert compiled[0].payload["materials"][0]["version"] == "v1"
    assert compiled[0].payload["materials"][0]["content"] == "version-one-reference"
    assert fixture.ledger.effect_count("orders") == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": "foreign"},
        {"application_id": "foreign"},
        {"resource_id": "record:foreign"},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"expires_at": datetime(2030, 1, 1)},
    ],
)
async def test_invalid_material_never_reaches_model_or_tool(pgcase, tmp_path, overrides):
    runtime, fixture, app, provider, owner, _, worker = await with_materials(pgcase, tmp_path)
    provider.overrides = overrides
    accepted = await runtime.accept(owner, app.deployment_id, "invalid-reference", "approve update")
    with pytest.raises(ScopeError):
        await runtime.advance(worker, accepted.run.run_id)
    assert fixture.ledger.all_receipts() == ()
    assert fixture.ledger.effect_count("orders") == 0
    view = await runtime.inspect(owner, accepted.run.run_id)
    assert view.checkpoint.status == "created"
    assert view.run.reserved_units == view.run.used_units == 0
    assert view.run.lease_until is None


async def test_provider_shape_changes_deployment_binding(pgcase, tmp_path):
    _, _, app, _, _, _, _ = await with_materials(pgcase, tmp_path)
    no_context = replace(app, application=replace(app.application, context_providers=()))
    assert no_context.binding_digest != app.binding_digest


@pytest.mark.parametrize("max_units,expected_units,expected_calls", [(0, 0, 0), (1_000, 7, 2)])
async def test_scalar_reservation_limit_exhausts_without_resending(
    pgcase, tmp_path, max_units, expected_units, expected_calls
):
    _, fixture, registration, owner, _, worker = await hosted_case(pgcase, tmp_path)
    app = replace(registration, max_run_units=max_units)
    runtime = HostedRuntime(pgcase.store, {app.deployment_id: app})
    accepted = await runtime.accept(owner, app.deployment_id, "unit-limit", "approve update")
    stopped = await runtime.advance(worker, accepted.run.run_id)
    assert stopped.run.status == stopped.checkpoint.status == "exhausted"
    assert stopped.run.used_units == expected_units
    assert stopped.run.reserved_units == 0
    assert len(fixture.ledger.all_receipts()) == expected_calls
    assert fixture.ledger.effect_count("orders") == 0
    trace = await runtime.trace(owner, accepted.run.run_id)
    assert len([event for event in trace.events if event.type == "execution.call.admitted"]) == (
        expected_calls
    )
    assert not any(event.type == "approval.consumed" for event in trace.events)
    assert trace.events[-2].type == "execution.budget_exhausted"
    replayed = await runtime.advance(worker, accepted.run.run_id)
    assert replayed == stopped
    assert len(fixture.ledger.all_receipts()) == expected_calls


async def test_crash_after_admission_is_fenced_proven_unsent_and_stopped(
    pgcase, tmp_path, monkeypatch
):
    from forgeharness.cloud.execution import HostedExecutionBackend
    from forgeharness.runtime.execution import ExecutionSuspended

    runtime, fixture, app, owner, _, worker = await hosted_case(pgcase, tmp_path)
    accepted = await runtime.accept(owner, app.deployment_id, "admission-crash", "approve update")

    async def crashed_before_send(self, envelope):
        raise ExecutionSuspended("synthetic crash after admission, before send permit")

    with monkeypatch.context() as patch:
        patch.setattr(HostedExecutionBackend, "_sent", crashed_before_send)
        paused = await runtime.advance(worker, accepted.run.run_id)
    assert paused.run.reserved_units == 1_000
    assert fixture.ledger.all_receipts() == ()
    stopped = await runtime.advance(worker, accepted.run.run_id)
    assert stopped.run.status == stopped.checkpoint.status == "failed"
    assert "confirmed not executed" in stopped.checkpoint.error
    assert stopped.run.reserved_units == stopped.run.used_units == 0
    assert stopped.checkpoint.usage.steps == 0
    assert fixture.ledger.all_receipts() == ()
    assert fixture.ledger.effect_count("orders") == 0

"""Trusted verification and recovery must precede hosted result settlement."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from forgeharness.cloud.evidence import BufferedEvidence
from forgeharness.cloud.execution import HostedExecutionBackend, governance_request, tool_envelope
from forgeharness.cloud.execution_calls import ExecutionCallFact
from forgeharness.cloud.execution_contract import CallEnvelope, PhysicalResult
from forgeharness.cloud.execution_state import ExecutionStateStore
from forgeharness.cloud.hosted import HostedApplication
from forgeharness.cloud.models import CallRecord, LeaseTicket, Receipt, TenantContext
from forgeharness.domain.models import (
    Actor,
    FinalAction,
    ModelResult,
    ModelUsage,
    ResourceScope,
    RunContext,
    RunResult,
    RunStatus,
    ToolCall,
    Usage,
)
from forgeharness.models.base import ModelRequest
from forgeharness.models.prepared import PreparedModelCall, canonical_json
from forgeharness.models.scripted import ScriptedModel
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.execution import ExecutionSuspended
from forgeharness.runtime.model_call import ModelCallKind, ModelCallOutcome
from forgeharness.runtime.verification import NoopVerifier
from forgeharness.sdk.policy import AuthorizationRequest
from forgeharness.sdk.spec import ApplicationSpec, ScopeError
from forgeharness.tools.base import ToolOutput
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import DispatchResult
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType


class AllowPolicy:
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="registered permission")

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool:
        return False


def configured(*, revoked=False):
    model_verifier, tool_verifier, resolver = AsyncMock(), AsyncMock(), AsyncMock()
    application = ApplicationSpec(
        id="hosted-test",
        version="1",
        policy_version="1",
        model_factory=lambda: ScriptedModel([]),
        tools=(EchoTool(),),
        policy=AllowPolicy(),
        verifier=NoopVerifier(),
        budget=RunBudget(max_attempts_per_call=1),
        authorize_run=lambda owner: not revoked,
    )
    registration = HostedApplication(
        application=application,
        deployment_id="hosted-test-v1",
        release_digest="a" * 64,
        model_binding_digest="b" * 64,
        max_run_units=20,
        model_reservation_units=2,
        tool_reservation_units={"echo": 1},
        model_verifier=model_verifier,
        tool_verifier=tool_verifier,
        resolver=resolver,
    )
    owner = RunContext(
        application_id=application.id,
        application_version=application.version,
        policy_version=application.policy_version,
        run_id="run-test",
        session_id="session-test",
        actor=Actor(id="owner", roles=registration.roles),
        scope=ResourceScope(tenant_id="org-test"),
        correlation_id="run-test",
    )
    context = TenantContext(org_id="org-test", actor_id="worker")
    ticket = LeaseTicket(
        org_id=context.org_id,
        run_id=owner.run_id,
        actor_id=context.actor_id,
        worker_id="worker-instance",
        epoch=2,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    checkpoint = RunResult(
        task_id=owner.run_id,
        status=RunStatus.RUNNING,
        run_context=owner,
        messages=(),
        usage=Usage(),
    )
    backend = HostedExecutionBackend(
        cast(ExecutionStateStore, SimpleNamespace(runs=SimpleNamespace())),
        context,
        ticket,
        registration,
        owner,
        BufferedEvidence(owner.run_id),
        checkpoint,
    )
    backend._bound = AsyncMock()
    backend._admit = AsyncMock()
    backend._sent = AsyncMock()
    backend.calls = SimpleNamespace(
        get=AsyncMock(return_value=None),
        mark_unknown=AsyncMock(),
        record_result=AsyncMock(),
        settle=AsyncMock(),
        retire_admission=AsyncMock(),
    )
    return backend, model_verifier, tool_verifier, resolver


def model_result(text="confirmed"):
    return ModelResult(
        action=FinalAction(content=text), usage=ModelUsage(input_tokens=3, output_tokens=2)
    )


def prepared():
    document = canonical_json(ModelRequest(task_id="run-test", messages=(), tools=()).model_dump())
    return PreparedModelCall(request_json=document, payload_json=document)


def model_physical():
    return PhysicalResult(
        receipt_id="verified-model",
        actual_units=1,
        kind="model",
        model_result=model_result(),
    )


def tool_physical(*, text="confirmed", elapsed_ms=5):
    return PhysicalResult(
        receipt_id="verified-tool",
        actual_units=1,
        kind="tool",
        tool_result=DispatchResult(output=ToolOutput(ok=True, content=text), elapsed_ms=elapsed_ms),
    )


async def invoke_model(backend, dispatch):
    return await backend.invoke_model(
        prepared=prepared(), usage=Usage(), phase="executor", dispatch=dispatch
    )


def fact(backend, envelope, *, result=None, state="unknown", admission_epoch=1, receipt=None):
    request = governance_request(envelope)
    return ExecutionCallFact(
        invocation=CallRecord(
            org_id=backend.context.org_id,
            run_id=backend.owner.run_id,
            **request.model_dump(),
            state=state,
            admission_epoch=admission_epoch,
        ),
        envelope=envelope,
        idempotency_key=backend._physical(envelope).idempotency_key,
        authorization_mode="policy",
        result=result,
        receipt=receipt,
    )


def model_envelope(backend):
    value = prepared()
    budget = backend.registered.application.budget
    return CallEnvelope(
        kind="model",
        logical_id="executor:0",
        canonical_input={
            "phase": "executor",
            "prepared": value.model_dump(mode="json"),
            "request": value.request.model_dump(mode="json"),
            "payload": value.payload,
        },
        reservation=Usage(
            steps=1, input_tokens=budget.max_input_tokens, output_tokens=budget.max_output_tokens
        ),
        reserve_units=backend.registered.model_reservation_units,
    )


async def test_model_result_is_settled_only_after_independent_exact_verification():
    backend, verifier, _, _ = configured()
    verified = model_physical()
    verifier.return_value = verified
    dispatch = AsyncMock(
        return_value=ModelCallOutcome(
            kind=ModelCallKind.SUCCESS,
            result=model_result(),
            usage=Usage(steps=1, input_tokens=3, output_tokens=2),
            response_received=True,
        )
    )

    outcome = await invoke_model(backend, dispatch)

    assert outcome.result == verified.model_result
    assert outcome.usage == verified.actual_usage
    dispatch.assert_awaited_once()
    backend.calls.record_result.assert_awaited_once_with(
        backend.context, backend.owner.run_id, model_envelope(backend).invocation_id, verified
    )
    backend.calls.settle.assert_awaited_once()
    backend.calls.mark_unknown.assert_not_awaited()


@pytest.mark.parametrize("answer", [None, "mismatched"])
async def test_unverified_model_response_suspends_with_reservation_intact(answer):
    backend, verifier, _, _ = configured()
    verifier.return_value = (
        None
        if answer is None
        else model_physical().model_copy(update={"model_result": model_result("different")})
    )
    dispatch = AsyncMock(
        return_value=ModelCallOutcome(
            kind=ModelCallKind.SUCCESS, usage=Usage(), result=model_result()
        )
    )

    with pytest.raises(ExecutionSuspended):
        await invoke_model(backend, dispatch)

    backend.calls.mark_unknown.assert_awaited_once()
    backend.calls.record_result.assert_not_awaited()
    backend.calls.settle.assert_not_awaited()


async def test_lost_model_response_never_becomes_a_free_model_failure():
    backend, verifier, _, _ = configured()
    dispatch = AsyncMock(side_effect=TimeoutError("response lost"))

    with pytest.raises(ExecutionSuspended):
        await invoke_model(backend, dispatch)

    verifier.assert_not_awaited()
    backend.calls.mark_unknown.assert_awaited_once()
    backend.calls.record_result.assert_not_awaited()
    backend.calls.settle.assert_not_awaited()


async def test_unknown_model_uses_verified_recovery_without_new_dispatch():
    backend, verifier, _, resolver = configured()
    envelope = model_envelope(backend)
    backend.calls.get.return_value = fact(backend, envelope)
    resolver.return_value = model_physical()
    dispatch = AsyncMock(side_effect=AssertionError("must never resend"))

    outcome = await invoke_model(backend, dispatch)

    assert outcome.result == model_result()
    resolver.assert_awaited_once()
    dispatch.assert_not_awaited()
    verifier.assert_not_awaited()
    backend._admit.assert_not_awaited()
    backend._sent.assert_not_awaited()


async def test_missing_recovery_result_preserves_unknown_and_never_resends():
    backend, _, _, resolver = configured()
    backend.calls.get.return_value = fact(backend, model_envelope(backend))
    resolver.return_value = None
    dispatch = AsyncMock(side_effect=AssertionError("must never resend"))

    with pytest.raises(ExecutionSuspended):
        await invoke_model(backend, dispatch)

    dispatch.assert_not_awaited()
    backend.calls.record_result.assert_not_awaited()
    backend.calls.settle.assert_not_awaited()
    backend.calls.mark_unknown.assert_awaited_once()


@pytest.mark.parametrize("match", [True, False])
async def test_tool_verification_binds_output_but_allows_independently_observed_duration(match):
    backend, _, verifier, _ = configured()
    call = ToolCall(id="tool-1", name="echo", arguments={"text": "hello"})
    backend.latest = backend.latest.model_copy(update={"authorized_call_id": call.id})
    verifier.return_value = tool_physical(text="confirmed" if match else "forged", elapsed_ms=0)
    dispatch = AsyncMock(return_value=tool_physical(elapsed_ms=35).tool_result)

    if match:
        output = await backend.invoke_tool(
            call=call,
            usage=Usage(),
            task_id=backend.owner.run_id,
            checkpoint_revision=0,
            dispatch=dispatch,
        )
        assert output.output.content == "confirmed"
        dispatch.assert_awaited_once_with(
            backend._physical(tool_envelope(backend.registered, call)).idempotency_key
        )
        backend.calls.record_result.assert_awaited_once()
        backend.calls.settle.assert_awaited_once()
    else:
        with pytest.raises(ExecutionSuspended):
            await backend.invoke_tool(
                call=call,
                usage=Usage(),
                task_id=backend.owner.run_id,
                checkpoint_revision=0,
                dispatch=dispatch,
            )
        backend.calls.record_result.assert_not_awaited()
        backend.calls.settle.assert_not_awaited()
        backend.calls.mark_unknown.assert_awaited_once()


async def test_revoked_owner_can_reconcile_existing_fees_but_cannot_send_again():
    backend, _, _, _ = configured(revoked=True)
    envelope = model_envelope(backend)
    result = await backend._resolve(fact(backend, envelope, result=model_physical()))
    assert result == model_physical()
    backend.calls.settle.assert_awaited_once()
    dispatch = AsyncMock()

    with pytest.raises(ScopeError, match="no longer authorized"):
        await invoke_model(backend, dispatch)

    dispatch.assert_not_awaited()
    backend._admit.assert_not_awaited()


async def test_old_unsent_admission_retires_without_provider_lookup_or_fake_output():
    backend, _, _, resolver = configured()
    envelope = model_envelope(backend)
    old = fact(backend, envelope, state="admitted", admission_epoch=1)

    with pytest.raises(ExecutionSuspended, match="no reusable runtime output"):
        await backend._resolve(old)

    backend.calls.retire_admission.assert_awaited_once()
    resolver.assert_not_awaited()
    backend.calls.record_result.assert_not_awaited()
    backend._sent.assert_not_awaited()


async def test_current_unsent_admission_is_never_retired_by_same_worker():
    backend, _, _, resolver = configured()
    envelope = model_envelope(backend)
    current = fact(backend, envelope, state="admitted", admission_epoch=2)

    with pytest.raises(ExecutionSuspended):
        await backend._resolve(current)

    backend.calls.retire_admission.assert_not_awaited()
    resolver.assert_not_awaited()


@pytest.mark.parametrize("state", ["admitted", "sent"])
async def test_reconciliation_finishes_known_nonexecution_without_fake_model_output(state):
    backend, _, _, resolver = configured()
    envelope = model_envelope(backend)
    receipt = (
        Receipt(
            receipt_id="known-nonexecution",
            actual_units=0,
            result_digest="f" * 64,
            outcome="not_executed",
        )
        if state == "sent"
        else None
    )
    backend.calls.get.return_value = fact(backend, envelope, state=state, receipt=receipt)
    connection = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"invocation_id": envelope.invocation_id}])
    )

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield connection

    backend.state.runs = SimpleNamespace(transaction=transaction, lock_lease=AsyncMock())

    await backend.reconcile_all()

    if state == "admitted":
        backend.calls.retire_admission.assert_awaited_once()
    else:
        backend.calls.settle.assert_awaited_once()
    resolver.assert_not_awaited()
    backend.calls.record_result.assert_not_awaited()

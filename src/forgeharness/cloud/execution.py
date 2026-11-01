"""PG-backed async execution boundary for the existing serial AgentRuntime.

No orchestration loop lives here. The trusted registration owns live application
authorization and independent physical-result verification. A lost or unverifiable
response suspends execution with its reservation intact; it never means free work.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from jsonschema import Draft202012Validator

from forgeharness.cloud.evidence import BufferedEvidence
from forgeharness.cloud.execution_calls import ExecutionCallFact, ExecutionCallStore
from forgeharness.cloud.execution_contract import (
    CallEnvelope,
    PhysicalResult,
    ProtocolErrorFact,
)
from forgeharness.cloud.execution_state import ExecutionStateStore
from forgeharness.cloud.hosted import HostedApplication, PhysicalCall
from forgeharness.cloud.models import (
    CallRequest,
    CloudConflict,
    CloudDenied,
    LeaseTicket,
    TenantContext,
)
from forgeharness.domain.models import ModelUsage, RunContext, RunResult, ToolCall, Usage
from forgeharness.models.base import ModelProtocolError
from forgeharness.models.prepared import PreparedModelCall
from forgeharness.runtime.execution import ExecutionSuspended
from forgeharness.runtime.model_call import ModelCallOutcome
from forgeharness.sdk.policy import AuthorizationRequest
from forgeharness.state.approval import ApprovalGrant, call_fingerprint
from forgeharness.tools.base import SchemaSource, ToolEffectClass, ToolSpec
from forgeharness.tools.dispatcher import DispatchResult
from forgeharness.tools.policy import PolicyDecisionType


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def tool_envelope(registered: HostedApplication, call: ToolCall) -> CallEnvelope:
    """Share the exact registered/schema-normalized approval and dispatch identity."""
    tool = next(
        (tool for tool in registered.application.tools if tool.spec.name == call.name), None
    )
    if tool is None:
        raise CloudDenied("hosted tool is not registered")
    if tool.spec.schema_source is SchemaSource.EXTERNAL_JSON_SCHEMA:
        Draft202012Validator(tool.spec.input_schema).validate(call.arguments)
    arguments = tool.input_model.model_validate(call.arguments).model_dump(mode="json")
    normalized = call.model_copy(update={"arguments": arguments}, deep=True)
    return CallEnvelope(
        kind="tool",
        logical_id=normalized.id,
        canonical_input={
            "call": normalized.model_dump(mode="json"),
            "tool_spec": tool.spec.model_dump(mode="json"),
        },
        reservation=Usage(tool_calls=1),
        reserve_units=registered.tool_reservation_units[call.name],
    )


def governance_request(envelope: CallEnvelope) -> CallRequest:
    if envelope.kind == "model":
        name, effect = "model.decide", "read"
    else:
        spec = ToolSpec.model_validate(envelope.canonical_input["tool_spec"])
        name = spec.name
        effect = {
            ToolEffectClass.READ_ONLY: "read",
            ToolEffectClass.IDEMPOTENT: "idempotent_write",
            ToolEffectClass.NON_IDEMPOTENT: "write",
        }[spec.effect_class]
    return CallRequest.model_validate(
        {
            "invocation_id": envelope.invocation_id,
            "tool_name": name,
            "effect_class": effect,
            "arguments_digest": envelope.arguments_digest,
            "reserve_units": envelope.reserve_units,
        }
    )


class HostedExecutionBackend:
    def __init__(
        self,
        state: ExecutionStateStore,
        context: TenantContext,
        ticket: LeaseTicket,
        registered: HostedApplication,
        owner: RunContext,
        trace: BufferedEvidence,
        checkpoint: RunResult,
    ) -> None:
        self.state, self.context, self.ticket = state, context, ticket
        self.registered = registered
        self.owner = owner.model_copy(deep=True)
        self.trace = trace
        self.latest = checkpoint.model_copy(deep=True)
        self.calls = ExecutionCallStore(state)
        self.binding_digest = registered.binding_digest
        # Lease liveness follows progress, not wall-clock survival: a worker that
        # is alive but stuck inside one call must not keep renewing a claim that
        # nothing is happening. ``last_progress`` is the timestamp of the last
        # completed unit of work.
        self.last_progress = time.monotonic()
        if (
            owner.run_id != ticket.run_id
            or owner.scope.tenant_id != context.org_id
            or ticket.org_id != context.org_id
            or ticket.actor_id != context.actor_id
            or checkpoint.task_id != owner.run_id
            or checkpoint.run_context != owner
        ):
            raise CloudDenied("hosted backend binding is inconsistent")
        # Revoking the owner prevents new effects, but cannot prevent a trusted
        # worker from reconciling fees/effects that were already admitted.
        registered.validate(self.owner, self.binding_digest, authorize=False)

    def _validate(self) -> None:
        self.registered.validate(self.owner, self.binding_digest)

    def _touch(self) -> None:
        self.last_progress = time.monotonic()

    async def release_lease(self) -> None:
        """Stop claiming liveness so another worker may reconcile this run."""
        await self.state.runs.release(self.context, self.ticket)

    async def _bound(self) -> None:
        async with self.state.runs.transaction(
            self.context, "execute", allow_inactive_org=True
        ) as connection:
            row = await self.state.runs.lock_lease(connection, self.context, self.ticket)
            run = self.state.runs.record(row)
            if (
                run.owner_id != self.owner.actor.id
                or run.definition.binding_digest != self.binding_digest
            ):
                raise CloudDenied("hosted deployment differs from the accepted execution")

    async def assert_current(self, previous: RunResult) -> None:
        await self._bound()
        async with self.state.runs.transaction(
            self.context, "execute", allow_inactive_org=True
        ) as connection:
            await self.state.runs.lock_lease(connection, self.context, self.ticket)
            row = await connection.fetchrow(
                "SELECT checkpoint FROM fh_cloud.execution_checkpoints "
                "WHERE org_id=$1 AND run_id=$2",
                self.context.org_id,
                self.owner.run_id,
            )
            if row is None or RunResult.model_validate(_decode(row["checkpoint"])) != previous:
                raise CloudConflict("hosted control checkpoint is no longer current")

    async def refresh(self) -> RunResult:
        """Reload committed control/evidence, without consuming a physical result."""
        async with self.state.runs.transaction(
            self.context, "execute", allow_inactive_org=True
        ) as connection:
            await self.state.runs.lock_lease(connection, self.context, self.ticket)
            row = await connection.fetchrow(
                "SELECT * FROM fh_cloud.execution_checkpoints "
                "WHERE org_id=$1 AND run_id=$2 FOR SHARE",
                self.context.org_id,
                self.owner.run_id,
            )
            if row is None:
                raise CloudDenied("hosted execution unavailable")
            history = await self.state._history(connection, self.context.org_id, self.owner.run_id)
            if len(history) != row["trace_sequence"]:
                raise CloudConflict("hosted evidence is incomplete")
            restored = BufferedEvidence.from_json(
                self.owner.run_id,
                json.dumps(history, ensure_ascii=False, separators=(",", ":")),
                transform=self.state.transform,
                expected_head=row["trace_hash"],
            )
            restored.ack_committed(restored.snapshot_pending())
            self.latest = RunResult.model_validate(_decode(row["checkpoint"]))
            self.trace = restored
            return self.latest

    async def save(self, result: RunResult) -> RunResult:
        await self._bound()
        if result.run_context != self.owner or result.task_id != self.owner.run_id:
            raise CloudDenied("hosted checkpoint owner changed")
        self.trace.append(
            "checkpoint.saved",
            {"revision": result.checkpoint_revision + 1, "status": result.status.value},
        )
        self.latest = await self.state.save(self.context, self.ticket, result, self.trace)
        self._touch()
        return self.latest

    def _tool(self, call: ToolCall) -> tuple[CallEnvelope, PolicyDecisionType]:
        self._validate()
        envelope = tool_envelope(self.registered, call)
        normalized = ToolCall.model_validate(envelope.canonical_input["call"])
        if normalized != call:
            raise CloudConflict("tool arguments must be normalized before hosted admission")
        decision = self.registered.application.policy.evaluate(
            AuthorizationRequest(
                context=self.owner,
                call=normalized,
                spec=ToolSpec.model_validate(envelope.canonical_input["tool_spec"]),
            )
        )
        if decision.type is PolicyDecisionType.DENY:
            raise CloudDenied("current owner policy denies the hosted tool")
        return envelope, decision.type

    def _physical(self, envelope: CallEnvelope) -> PhysicalCall:
        return PhysicalCall(
            owner=self.owner.model_copy(deep=True),
            envelope=envelope.model_copy(deep=True),
            idempotency_key=envelope.idempotency_key(
                self.context.org_id, self.owner.run_id, self.binding_digest
            ),
        )

    async def _cached(self, envelope: CallEnvelope) -> ExecutionCallFact | None:
        fact = await self.calls.get(self.context, self.owner.run_id, envelope.invocation_id)
        if fact is None:
            return None
        expected = governance_request(envelope)
        if (
            fact.envelope != envelope
            or fact.idempotency_key != self._physical(envelope).idempotency_key
            or any(
                getattr(fact.invocation, key) != value
                for key, value in expected.model_dump().items()
            )
        ):
            raise CloudConflict("cached physical call differs from the exact execution")
        return fact

    async def consume_approval(
        self,
        grant: ApprovalGrant,
        *,
        previous: RunResult,
        candidate: RunResult,
        call: ToolCall,
    ) -> RunResult:
        await self.assert_current(previous)
        envelope, _ = self._tool(call)
        approver = grant.approver_context
        if (
            grant.task_id != self.owner.run_id
            or grant.binding_digest != self.binding_digest
            or grant.call_fingerprint != call_fingerprint(self.owner.run_id, call)
            or approver is None
            or grant.granted_by != approver.actor.id
            or approver.actor.id == self.owner.actor.id
            or approver.run_id != self.owner.run_id
            or approver.scope != self.owner.scope
            or (approver.application_id, approver.application_version, approver.policy_version)
            != (
                self.owner.application_id,
                self.owner.application_version,
                self.owner.policy_version,
            )
            or not self.registered.application.policy.can_approve(
                requester=approver, owner=self.owner, call=call
            )
        ):
            raise CloudDenied("hosted approval metadata or current permission changed")
        async with self.state.runs.transaction(self.context, "execute") as connection:
            row = await connection.fetchrow(
                "SELECT run_id,approver_id,expires_at FROM fh_cloud.approvals "
                "WHERE org_id=$1 AND approval_id=$2",
                self.context.org_id,
                grant.id,
            )
            if row is None or (
                row["run_id"] != grant.task_id
                or row["approver_id"] != grant.granted_by
                or row["expires_at"] != grant.expires_at
            ):
                raise CloudDenied("hosted approval was not issued for these exact metadata")
        self.trace.append("approval.consumed", {"tool": call.name, "granted_by": grant.granted_by})
        self.trace.append(
            "checkpoint.saved",
            {"revision": candidate.checkpoint_revision + 1, "status": candidate.status.value},
        )
        batch = self.trace.snapshot_pending()
        _, saved = await self.calls.admit(
            self.context,
            self.ticket,
            envelope,
            request=governance_request(envelope),
            approval_id=grant.id,
            checkpoint=candidate,
            expected_revision=previous.checkpoint_revision,
            batch=batch,
        )
        if saved is None:
            raise CloudConflict("approval admission did not commit its control checkpoint")
        self.trace.ack_committed(batch)
        self.latest = saved
        self._touch()
        return saved

    async def _admit(self, envelope: CallEnvelope) -> None:
        physical = self._physical(envelope)
        self.trace.append(
            "execution.call.admitted",
            {
                "invocation_id": envelope.invocation_id,
                "kind": envelope.kind,
                "idempotency_key": physical.idempotency_key,
            },
        )
        batch = self.trace.snapshot_pending()
        await self.calls.admit(
            self.context,
            self.ticket,
            envelope,
            request=governance_request(envelope),
            expected_revision=self.latest.checkpoint_revision,
            batch=batch,
        )
        self.trace.ack_committed(batch)

    async def _sent(self, envelope: CallEnvelope) -> None:
        self.trace.append("execution.call.sending", {"invocation_id": envelope.invocation_id})
        batch = self.trace.snapshot_pending()
        await self.calls.mark_sent(self.context, self.ticket, envelope.invocation_id, batch=batch)
        self.trace.ack_committed(batch)

    async def _settle(self, envelope: CallEnvelope, result: PhysicalResult) -> PhysicalResult:
        self.trace.append(
            "execution.call.result",
            {"invocation_id": envelope.invocation_id, "result": result.model_dump(mode="json")},
        )
        batch = self.trace.snapshot_pending()
        await self.calls.settle(self.context, self.ticket, envelope.invocation_id, batch=batch)
        self.trace.ack_committed(batch)
        self._touch()
        return result

    async def _retire(self, fact: ExecutionCallFact) -> None:
        self.trace.append(
            "execution.call.unsent",
            {
                "invocation_id": fact.envelope.invocation_id,
                "admission_epoch": fact.invocation.admission_epoch,
                "retirement_epoch": self.ticket.epoch,
            },
        )
        batch = self.trace.snapshot_pending()
        await self.calls.retire_admission(
            self.context, self.ticket, fact.envelope.invocation_id, batch=batch
        )
        self.trace.ack_committed(batch)
        self._touch()

    async def _unknown(self, envelope: CallEnvelope) -> None:
        self.trace.append("execution.call.unknown", {"invocation_id": envelope.invocation_id})
        batch = self.trace.snapshot_pending()
        try:
            await self.calls.mark_unknown(
                self.context, self.ticket, envelope.invocation_id, batch=batch
            )
        except Exception:
            # An expired worker cannot project uncertainty, but its durable sent
            # row and reservation already force the current owner to reconcile.
            pass
        else:
            self.trace.ack_committed(batch)
        raise ExecutionSuspended("physical result requires trusted reconciliation")

    async def _resolve(self, fact: ExecutionCallFact) -> PhysicalResult:
        if fact.receipt is not None and fact.receipt.outcome == "not_executed":
            await self.calls.settle(self.context, self.ticket, fact.envelope.invocation_id)
            raise ExecutionSuspended("confirmed nonexecution has no reusable runtime output")
        if fact.result is not None:
            return await self._settle(fact.envelope, fact.result)
        if fact.invocation.state == "admitted":
            if fact.invocation.admission_epoch < self.ticket.epoch:
                await self._retire(fact)
            raise ExecutionSuspended("unsent admission has no reusable runtime output")
        resolver = self.registered.resolver
        if resolver is None:
            await self._unknown(fact.envelope)
        assert resolver is not None
        try:
            result = await resolver(self._physical(fact.envelope))
            if result is not None:
                result = PhysicalResult.model_validate(result, strict=True)
                if result.kind != fact.envelope.kind:
                    result = None
        except Exception:
            result = None
        if result is None:
            await self._unknown(fact.envelope)
        assert result is not None
        await self.calls.record_result(
            self.context, self.owner.run_id, fact.envelope.invocation_id, result
        )
        return await self._settle(fact.envelope, result)

    @staticmethod
    def _model_matches(result: PhysicalResult, observed: ModelCallOutcome) -> bool:
        if result.kind != "model":
            return False
        if result.model_result is not None:
            return observed.result is not None and result.model_result == observed.result
        if result.protocol_error is None or not isinstance(observed.violation, ModelProtocolError):
            return False
        error = observed.violation
        if error.usage is None:
            return False
        fact = ProtocolErrorFact(
            code=error.code,
            message=str(error),
            received_tool_calls=error.received_tool_calls,
            recoverable=error.recoverable,
            usage=ModelUsage.model_validate(error.usage),
        )
        return fact == result.protocol_error

    async def invoke_model(
        self,
        *,
        prepared: PreparedModelCall,
        usage: Usage,
        phase: str,
        dispatch: Callable[[], Awaitable[ModelCallOutcome]],
    ) -> ModelCallOutcome:
        self._validate()
        await self._bound()
        self._touch()
        if usage != self.latest.usage:
            raise CloudConflict("model usage differs from the current control cursor")
        budget = self.registered.application.budget
        envelope = CallEnvelope(
            kind="model",
            logical_id=f"{phase}:{usage.steps}",
            canonical_input={
                "phase": phase,
                "prepared": prepared.model_dump(mode="json"),
                "request": prepared.request.model_dump(mode="json"),
                "payload": prepared.payload,
            },
            reservation=Usage(
                steps=1,
                input_tokens=max(0, budget.max_input_tokens - usage.input_tokens),
                output_tokens=max(0, budget.max_output_tokens - usage.output_tokens),
            ),
            reserve_units=self.registered.model_reservation_units,
        )
        cached = await self._cached(envelope)
        if cached is not None:
            return (await self._resolve(cached)).restore_model(usage)
        await self._admit(envelope)
        self._validate()
        await self._sent(envelope)
        try:
            self._validate()
            observed = await dispatch()
            result = await self.registered.model_verifier(self._physical(envelope), observed)
            if result is not None:
                result = PhysicalResult.model_validate(result, strict=True)
                if not self._model_matches(result, observed):
                    result = None
        except (Exception, asyncio.CancelledError):
            await self._unknown(envelope)
        if result is None:
            await self._unknown(envelope)
        assert result is not None
        await self.calls.record_result(
            self.context, self.owner.run_id, envelope.invocation_id, result
        )
        settled = (await self._settle(envelope, result)).restore_model(usage)
        self._touch()
        return settled

    async def invoke_tool(
        self,
        *,
        call: ToolCall,
        usage: Usage,
        task_id: str,
        checkpoint_revision: int,
        dispatch: Callable[[str], Awaitable[DispatchResult]],
    ) -> DispatchResult:
        envelope, decision = self._tool(call)
        await self._bound()
        self._touch()
        if (
            task_id != self.owner.run_id
            or checkpoint_revision != self.latest.checkpoint_revision
            or usage != self.latest.usage
            or self.latest.authorized_call_id != call.id
        ):
            raise CloudConflict("tool dispatch differs from checkpointed control authorization")
        cached = await self._cached(envelope)
        if cached is not None and cached.result is not None:
            resolved = await self._resolve(cached)
            assert resolved.tool_result is not None
            return resolved.tool_result
        if cached is not None and cached.invocation.state != "admitted":
            resolved = await self._resolve(cached)
            assert resolved.tool_result is not None
            return resolved.tool_result
        if decision is PolicyDecisionType.REQUIRE_APPROVAL and (
            cached is None or cached.authorization_mode != "approval"
        ):
            raise CloudDenied("hosted tool needs an independent exact approval")
        if cached is None:
            await self._admit(envelope)
        # A previously admitted call may only use the same current admission epoch.
        elif cached.invocation.admission_epoch != self.ticket.epoch:
            raise ExecutionSuspended("old execution admission cannot be dispatched by a new owner")
        self._tool(call)
        await self._sent(envelope)
        try:
            self._tool(call)
            observed = await dispatch(self._physical(envelope).idempotency_key)
            result = await self.registered.tool_verifier(self._physical(envelope), observed)
            if result is not None:
                result = PhysicalResult.model_validate(result, strict=True)
                if (
                    result.kind != "tool"
                    or result.tool_result is None
                    or result.tool_result.output != observed.output
                ):
                    result = None
        except (Exception, asyncio.CancelledError):
            await self._unknown(envelope)
        if result is None:
            await self._unknown(envelope)
        assert result is not None and result.tool_result is not None
        await self.calls.record_result(
            self.context, self.owner.run_id, envelope.invocation_id, result
        )
        await self._settle(envelope, result)
        self._touch()
        return result.tool_result

    async def recover_tool(self, *, task_id: str, call: ToolCall) -> DispatchResult | None:
        envelope, _ = self._tool(call)
        await self._bound()
        self._touch()
        if task_id != self.owner.run_id:
            raise CloudDenied("hosted recovery task differs from its binding")
        fact = await self._cached(envelope)
        if fact is None:
            return None
        result = await self._resolve(fact)
        if result.tool_result is None:
            raise CloudConflict("tool recovery returned a model result")
        return result.tool_result

    async def reconcile_all(self) -> None:
        """Reconcile physical facts only; controller decides whether control can run."""
        async with self.state.runs.transaction(
            self.context, "execute", allow_inactive_org=True
        ) as connection:
            await self.state.runs.lock_lease(connection, self.context, self.ticket)
            identifiers = await connection.fetch(
                "SELECT invocation_id FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND state<>'settled' ORDER BY invocation_id",
                self.context.org_id,
                self.owner.run_id,
            )
        for identifier in identifiers:
            fact = await self.calls.get(
                self.context, self.owner.run_id, identifier["invocation_id"]
            )
            if fact is None:
                raise ExecutionSuspended("execution identity is missing its physical facts")
            if fact.receipt is not None and fact.receipt.outcome == "not_executed":
                await self.calls.settle(self.context, self.ticket, fact.envelope.invocation_id)
                self._touch()
                continue
            if fact.invocation.state == "admitted":
                if fact.invocation.admission_epoch >= self.ticket.epoch:
                    raise ExecutionSuspended("current admission cannot be retired during recovery")
                await self._retire(fact)
                continue
            await self._resolve(fact)

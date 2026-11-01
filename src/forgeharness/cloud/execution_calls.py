"""Atomic PG admission and complete physical facts for trusted hosted services.

Policy admission is an internal service capability, never a tenant HTTP input.
The host evaluates its registered policy before admitting/sending and authenticates
provider/tool facts before record_result. Unknown execution never releases budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from forgeharness.cloud.calls import PostgresCallStore
from forgeharness.cloud.execution_contract import (
    CallEnvelope,
    PhysicalResult,
    canon_result_digest,
)
from forgeharness.cloud.execution_state import ExecutionStateStore
from forgeharness.cloud.models import (
    CallRecord,
    CallRequest,
    CloudBudgetExceeded,
    CloudConflict,
    CloudDenied,
    LeaseTicket,
    Receipt,
    TenantContext,
    approval_fingerprint,
)
from forgeharness.domain.models import RunResult, RunStatus, ToolCall, Usage
from forgeharness.runtime.budget import RunBudget
from forgeharness.tools.base import ToolEffectClass, ToolSpec


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _usage(value: Any) -> Usage:
    return Usage.model_validate_json(_json(_decode(value)), strict=True)


def _sum(left: Usage, right: Usage) -> Usage:
    return Usage(**{key: getattr(left, key) + getattr(right, key) for key in Usage.model_fields})


def _subtract(left: Usage, right: Usage) -> Usage:
    if any(getattr(left, key) < getattr(right, key) for key in Usage.model_fields):
        raise CloudConflict("execution reservation projection is inconsistent")
    return Usage(**{key: getattr(left, key) - getattr(right, key) for key in Usage.model_fields})


@dataclass(frozen=True)
class ExecutionCallFact:
    invocation: CallRecord
    envelope: CallEnvelope
    idempotency_key: str
    authorization_mode: str
    result: PhysicalResult | None
    receipt: Receipt | None


_SELECT = """
SELECT i.*,c.kind,c.logical_id,c.idempotency_key,c.canonical_input,
       c.reserved_usage AS call_reservation,c.canonical_output,
       c.actual_usage AS call_actual_usage
FROM fh_cloud.invocations i JOIN fh_cloud.execution_calls c USING(org_id,run_id,invocation_id)
WHERE i.org_id=$1 AND i.run_id=$2 AND i.invocation_id=$3
"""


class ExecutionCallStore:
    def __init__(self, state: ExecutionStateStore) -> None:
        self.state = state
        self.runs = state.runs

    async def _checkpoint(self, connection: Any, context: TenantContext, run_id: str) -> Any:
        row = await connection.fetchrow(
            "SELECT * FROM fh_cloud.execution_checkpoints WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
            context.org_id,
            run_id,
        )
        if row is None:
            raise CloudDenied("execution unavailable")
        return row

    async def _active(self, connection: Any, context: TenantContext, ticket: LeaseTicket) -> Any:
        run = await self.runs.lock_lease(connection, context, ticket)
        await PostgresCallStore._owner_active(connection, context.org_id, run["owner_id"])
        await self.runs.authorize_bound_run(connection, context.org_id, ticket.run_id)
        # Owner/approval/checkpoint row locks may have consumed the lease or deadline.
        run = await self.runs.lock_lease(connection, context, ticket)
        if (
            run["status"] != "running"
            or run["cancel_requested"]
            or self.runs.record(run).definition.deadline_at <= run["db_now"]
        ):
            raise CloudConflict("execution no longer accepts a physical call")
        return run

    @staticmethod
    def _budget(run: Any, checkpoint: Any, reservation: Usage, units: int) -> None:
        budget = RunBudget.model_validate(_decode(checkpoint["budget"]))
        usage = _sum(
            _sum(_usage(checkpoint["used_usage"]), _usage(checkpoint["reserved_usage"])),
            reservation,
        )
        limits = {
            "steps": budget.max_steps,
            "tool_calls": budget.max_tool_calls,
            "input_tokens": budget.max_input_tokens,
            "output_tokens": budget.max_output_tokens,
        }
        immutable = _decode(run["definition"])
        if run["used_units"] + run["reserved_units"] + units > immutable["max_units"] or any(
            getattr(usage, key) > limit for key, limit in limits.items()
        ):
            raise CloudBudgetExceeded("execution budget exhausted")

    @staticmethod
    def _request(envelope: CallEnvelope, request: CallRequest) -> None:
        if (
            request.invocation_id != envelope.invocation_id
            or request.arguments_digest != envelope.arguments_digest
            or request.reserve_units != envelope.reserve_units
            or (
                envelope.kind == "model"
                and (request.effect_class != "read" or request.tool_name != "model.decide")
            )
        ):
            raise CloudConflict("governance request differs from the exact execution envelope")
        if envelope.kind == "tool":
            call = ToolCall.model_validate(envelope.canonical_input.get("call"))
            spec = ToolSpec.model_validate(envelope.canonical_input.get("tool_spec"))
            effects = {
                ToolEffectClass.READ_ONLY: "read",
                ToolEffectClass.IDEMPOTENT: "idempotent_write",
                ToolEffectClass.NON_IDEMPOTENT: "write",
            }
            if (
                call.id != envelope.logical_id
                or call.name != request.tool_name
                or spec.name != call.name
                or effects[spec.effect_class] != request.effect_class
            ):
                raise CloudConflict("registered tool declaration differs from execution governance")

    async def _approval(
        self,
        connection: Any,
        context: TenantContext,
        run: Any,
        request: CallRequest,
        approval_id: str,
        *,
        consumed: bool,
    ) -> None:
        grant = await connection.fetchrow(
            "SELECT * FROM fh_cloud.approvals WHERE org_id=$1 AND approval_id=$2 FOR UPDATE",
            context.org_id,
            approval_id,
        )
        if grant is None:
            raise CloudDenied("approval unavailable")
        await self.runs.authorize_bound_run(
            connection,
            context.org_id,
            run["run_id"],
            approver_id=grant["approver_id"],
        )
        approver = await connection.fetchrow(
            "SELECT active,can_approve FROM fh_cloud.members "
            "WHERE org_id=$1 AND actor_id=$2 FOR SHARE",
            context.org_id,
            grant["approver_id"],
        )
        now = await connection.fetchval("SELECT clock_timestamp()")
        if (
            grant["run_id"] != run["run_id"]
            or grant["action_digest"] != approval_fingerprint(self.runs.record(run), request)
            or grant["consumed"] != consumed
            or grant["expires_at"] <= now
            or grant["approver_id"] == run["owner_id"]
            or approver is None
            or not approver["active"]
            or not approver["can_approve"]
        ):
            raise CloudDenied("approval is no longer valid for the exact execution")

    async def admit(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        envelope: CallEnvelope,
        *,
        request: CallRequest,
        approval_id: str | None = None,
        checkpoint: RunResult | None = None,
        expected_revision: int,
        batch: tuple[str, ...] = (),
    ) -> tuple[CallRecord, RunResult | None]:
        """Trusted policy/approval admission; no network calls inside this transaction."""
        envelope = CallEnvelope.model_validate(envelope, strict=True)
        request = CallRequest.model_validate_json(request.model_dump_json(), strict=True)
        self._request(envelope, request)
        self.state._clean(envelope.model_dump(mode="json"))
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected revision needs a nonnegative integer")
        async with self.runs.transaction(context, "execute") as connection:
            run = await self.runs.lock_lease(connection, context, ticket)
            current = await self._checkpoint(connection, context, ticket.run_id)
            run = await self._active(connection, context, ticket)
            if current["revision"] != expected_revision:
                raise CloudConflict("execution checkpoint revision changed before admission")
            prior = await connection.fetchrow(
                _SELECT, context.org_id, ticket.run_id, envelope.invocation_id
            )
            if prior is not None:
                fact = await self._fact(connection, prior)
                if (
                    fact.envelope != envelope
                    or prior["approval_id"] != approval_id
                    or any(prior[key] != value for key, value in request.model_dump().items())
                ):
                    raise CloudConflict("logical execution identity was used for another action")
                if checkpoint is not None:
                    raise CloudConflict(
                        "approval admission was already committed; reload checkpoint"
                    )
                await self.state.commit_trace_in_transaction(connection, context, ticket, batch)
                return fact.invocation, None
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3)",
                context.org_id,
                ticket.run_id,
                envelope.invocation_id,
            ):
                raise CloudConflict("invocation already belongs to another execution contract")
            if envelope.kind == "tool" and await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.execution_logical_usage "
                "WHERE org_id=$1 AND run_id=$2 AND logical_id=$3)",
                context.org_id,
                ticket.run_id,
                envelope.logical_id,
            ):
                raise CloudConflict("logical tool identity was already rejected")
            count = await connection.fetchval(
                "SELECT (SELECT COUNT(*) FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2) + "
                "(SELECT COUNT(*) FROM fh_cloud.execution_logical_usage "
                "WHERE org_id=$1 AND run_id=$2)",
                context.org_id,
                ticket.run_id,
            )
            if count >= self.runs.record(run).definition.max_calls:
                raise CloudBudgetExceeded("execution call budget exhausted")
            self._budget(run, current, envelope.reservation, envelope.reserve_units)
            if approval_id is not None:
                await self._approval(connection, context, run, request, approval_id, consumed=False)
            saved = None
            previous = RunResult.model_validate(_decode(current["checkpoint"]))
            if previous.status is RunStatus.AWAITING_APPROVAL and checkpoint is None:
                raise CloudConflict("pending approval requires atomic checkpoint admission")
            if checkpoint is not None:
                pending = previous.pending_approval
                if (
                    approval_id is None
                    or envelope.kind != "tool"
                    or previous.status is not RunStatus.AWAITING_APPROVAL
                    or pending is None
                    or pending.call.id != envelope.logical_id
                    or checkpoint.status is not RunStatus.RUNNING
                    or checkpoint.pending_approval is not None
                    or checkpoint.authorized_call_id != pending.call.id
                    or checkpoint.messages != previous.messages
                    or checkpoint.usage != previous.usage
                    or checkpoint.tool_evidence != previous.tool_evidence
                ):
                    raise CloudConflict("checkpoint is not an exact approval admission")
                expected_call = ToolCall.model_validate(envelope.canonical_input["call"])
                if expected_call != pending.call:
                    raise CloudConflict("approved tool differs from the pending execution")
                saved = await self.state.save_in_transaction(
                    connection, context, ticket, checkpoint, batch
                )
            else:
                await self.state.commit_trace_in_transaction(connection, context, ticket, batch)
            run = await self._active(connection, context, ticket)
            if approval_id is not None:
                # Persisting a large evidence batch may outlive the approval even
                # while its row/member locks prevent concurrent revocation.
                await self._approval(connection, context, run, request, approval_id, consumed=False)
                await self._active(connection, context, ticket)
                await connection.execute(
                    "UPDATE fh_cloud.approvals SET consumed=true "
                    "WHERE org_id=$1 AND approval_id=$2",
                    context.org_id,
                    approval_id,
                )
            row = await connection.fetchrow(
                "INSERT INTO fh_cloud.invocations "
                "(org_id,run_id,invocation_id,tool_name,effect_class,arguments_digest,"
                "action_digest,approval_id,authorization_mode,reserve_units,state,admission_epoch) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'admitted',$11) RETURNING *",
                context.org_id,
                ticket.run_id,
                envelope.invocation_id,
                request.tool_name,
                request.effect_class,
                request.arguments_digest,
                approval_fingerprint(self.runs.record(run), request),
                approval_id,
                "approval" if approval_id is not None else "policy",
                envelope.reserve_units,
                ticket.epoch,
            )
            await connection.execute(
                "INSERT INTO fh_cloud.execution_calls "
                "(org_id,run_id,invocation_id,kind,logical_id,idempotency_key,"
                "canonical_input,reserved_usage) "
                "VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)",
                context.org_id,
                ticket.run_id,
                envelope.invocation_id,
                envelope.kind,
                envelope.logical_id,
                envelope.idempotency_key(
                    context.org_id, ticket.run_id, self.runs.record(run).definition.binding_digest
                ),
                _json(envelope.canonical_input),
                envelope.reservation.model_dump_json(),
            )
            await connection.execute(
                "UPDATE fh_cloud.runs SET reserved_units=reserved_units+$3 "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
                envelope.reserve_units,
            )
            await connection.execute(
                "UPDATE fh_cloud.execution_checkpoints SET reserved_usage=$3::jsonb "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
                _sum(_usage(current["reserved_usage"]), envelope.reservation).model_dump_json(),
            )
            await self.runs.emit(
                connection,
                context.org_id,
                ticket.run_id,
                "execution.call_admitted",
                {"invocation_id": envelope.invocation_id, "kind": envelope.kind},
            )
            return PostgresCallStore.record(row), saved

    async def mark_sent(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        invocation_id: str,
        *,
        batch: tuple[str, ...] = (),
    ) -> CallRecord:
        async with self.runs.transaction(context, "execute") as connection:
            await self.runs.lock_lease(connection, context, ticket)
            current = await self._checkpoint(connection, context, ticket.run_id)
            run = await self._active(connection, context, ticket)
            call = await connection.fetchrow(
                _SELECT + " FOR UPDATE OF i,c", context.org_id, ticket.run_id, invocation_id
            )
            if call is None:
                raise CloudDenied("execution call unavailable")
            receipt = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.receipts "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3)",
                context.org_id,
                ticket.run_id,
                invocation_id,
            )
            if (
                call["state"] != "admitted"
                or call["admission_epoch"] != ticket.epoch
                or call["canonical_output"] is not None
                or receipt
            ):
                raise CloudConflict("execution cannot send or already has a final fact")
            if call["authorization_mode"] == "approval":
                request = CallRequest.model_validate(
                    {key: call[key] for key in CallRequest.model_fields}
                )
                await self._approval(
                    connection, context, run, request, call["approval_id"], consumed=True
                )
            elif call["authorization_mode"] != "policy" or call["approval_id"] is not None:
                raise CloudConflict("execution authorization mode is inconsistent")
            await self.state.commit_trace_in_transaction(connection, context, ticket, batch)
            run = await self._active(connection, context, ticket)
            if call["authorization_mode"] == "approval":
                await self._approval(
                    connection, context, run, request, call["approval_id"], consumed=True
                )
                run = await self._active(connection, context, ticket)
            self._budget(run, current, Usage(), 0)
            row = await connection.fetchrow(
                "UPDATE fh_cloud.invocations SET state='sent' WHERE org_id=$1 AND run_id=$2 "
                "AND invocation_id=$3 RETURNING *",
                context.org_id,
                ticket.run_id,
                invocation_id,
            )
            await self.runs.emit(
                connection,
                context.org_id,
                ticket.run_id,
                "execution.call_sent",
                {"invocation_id": invocation_id},
            )
            return PostgresCallStore.record(row)

    async def record_result(
        self,
        context: TenantContext,
        run_id: str,
        invocation_id: str,
        result: PhysicalResult,
    ) -> None:
        """Append an independently authenticated physical fact, without a lease."""
        result = PhysicalResult.model_validate(result, strict=True)
        self.state._clean(result.model_dump(mode="json"))
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            if not await connection.fetchval(
                "SELECT 1 FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR KEY SHARE",
                context.org_id,
                run_id,
            ):
                raise CloudDenied("execution call unavailable")
            call = await connection.fetchrow(
                _SELECT + " FOR UPDATE OF i,c", context.org_id, run_id, invocation_id
            )
            if call is None:
                raise CloudDenied("execution call unavailable")
            if call["state"] == "admitted" or call["kind"] != result.kind:
                raise CloudConflict("unstarted or mismatched execution cannot report a result")
            if call["canonical_output"] is not None:
                original = PhysicalResult.model_validate_json(
                    _json(_decode(call["canonical_output"])), strict=True
                )
                if (
                    canon_result_digest(original) != canon_result_digest(result)
                    or _usage(call["call_actual_usage"]) != result.actual_usage
                ):
                    raise CloudConflict("physical result conflicts with an immutable fact")
            receipt = Receipt(
                receipt_id=result.receipt_id,
                actual_units=result.actual_units,
                result_digest=canon_result_digest(result),
            )
            await connection.execute(
                "INSERT INTO fh_cloud.receipts "
                "(org_id,run_id,invocation_id,receipt_id,actual_units,result_digest,outcome) "
                "VALUES($1,$2,$3,$4,$5,$6,'completed') ON CONFLICT DO NOTHING",
                context.org_id,
                run_id,
                invocation_id,
                receipt.receipt_id,
                receipt.actual_units,
                receipt.result_digest,
            )
            stored = await connection.fetchrow(
                "SELECT * FROM fh_cloud.receipts "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                context.org_id,
                run_id,
                invocation_id,
            )
            if stored is None or any(
                stored[key] != value for key, value in receipt.model_dump().items()
            ):
                raise CloudConflict("physical receipt conflicts with an immutable fact")
            if call["canonical_output"] is None:
                await connection.execute(
                    "UPDATE fh_cloud.execution_calls SET "
                    "canonical_output=$4::jsonb,actual_usage=$5::jsonb "
                    "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                    context.org_id,
                    run_id,
                    invocation_id,
                    result.model_dump_json(),
                    result.actual_usage.model_dump_json(),
                )
            await self.runs.emit(
                connection,
                context.org_id,
                run_id,
                "execution.result_recorded",
                {"invocation_id": invocation_id, "result_digest": receipt.result_digest},
                event_id=f"execution-result:{result.receipt_id}",
            )

    async def retire_admission(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        invocation_id: str,
        *,
        batch: tuple[str, ...] = (),
    ) -> CallRecord:
        """Retire an old, fenced admission that never committed permission to send.

        The send transition is a prerequisite to entering a provider/tool. Holding
        the current run lease and call locks proves that an older admitted epoch
        can no longer make that transition. This proof applies only to admitted
        calls: a sent or unknown call must retain its reservation until a trusted
        physical result or independent nonexecution receipt is available.
        """
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            await self.runs.lock_lease(connection, context, ticket)
            current = await self._checkpoint(connection, context, ticket.run_id)
            call = await connection.fetchrow(
                _SELECT + " FOR UPDATE OF i,c", context.org_id, ticket.run_id, invocation_id
            )
            if call is None:
                raise CloudDenied("execution call unavailable")
            if call["state"] != "admitted" or call["admission_epoch"] >= ticket.epoch:
                raise CloudConflict("only an older unsent admission can be retired")
            if call["canonical_output"] is not None:
                raise CloudConflict("unsent admission conflicts with a physical result")
            proof = {
                "kind": "fenced-unsent-admission-v1",
                "org_id": context.org_id,
                "run_id": ticket.run_id,
                "invocation_id": invocation_id,
                "admission_epoch": call["admission_epoch"],
            }
            digest = hashlib.sha256(_json(proof).encode("utf-8")).hexdigest()
            receipt = Receipt(
                receipt_id=f"unsent-{digest}",
                actual_units=0,
                result_digest=digest,
                outcome="not_executed",
            )
            existing = await connection.fetchrow(
                "SELECT * FROM fh_cloud.receipts "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                context.org_id,
                ticket.run_id,
                invocation_id,
            )
            if existing is not None and any(
                existing[key] != value for key, value in receipt.model_dump().items()
            ):
                raise CloudConflict("unsent proof conflicts with an immutable receipt")
            reserved = _subtract(
                _usage(current["reserved_usage"]), _usage(call["call_reservation"])
            )
            await self.state.commit_trace_in_transaction(connection, context, ticket, batch)
            await self.runs.lock_lease(connection, context, ticket)
            await connection.execute(
                "INSERT INTO fh_cloud.receipts "
                "(org_id,run_id,invocation_id,receipt_id,actual_units,result_digest,outcome) "
                "VALUES($1,$2,$3,$4,0,$5,'not_executed') ON CONFLICT DO NOTHING",
                context.org_id,
                ticket.run_id,
                invocation_id,
                receipt.receipt_id,
                receipt.result_digest,
            )
            await connection.execute(
                "UPDATE fh_cloud.runs SET reserved_units=reserved_units-$3 "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
                call["reserve_units"],
            )
            await connection.execute(
                "UPDATE fh_cloud.execution_checkpoints SET reserved_usage=$3::jsonb "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
                reserved.model_dump_json(),
            )
            await connection.execute(
                "UPDATE fh_cloud.execution_calls SET actual_usage=$4::jsonb "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                context.org_id,
                ticket.run_id,
                invocation_id,
                Usage().model_dump_json(),
            )
            row = await connection.fetchrow(
                "UPDATE fh_cloud.invocations SET state='settled',receipt_id=$4 "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 RETURNING *",
                context.org_id,
                ticket.run_id,
                invocation_id,
                receipt.receipt_id,
            )
            await self.runs.emit(
                connection,
                context.org_id,
                ticket.run_id,
                "execution.admission_retired",
                {**proof, "retirement_epoch": ticket.epoch, "result_digest": digest},
                event_id=f"execution-unsent:{receipt.receipt_id}",
            )
            return PostgresCallStore.record(row)

    async def settle(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        invocation_id: str,
        *,
        batch: tuple[str, ...] = (),
    ) -> CallRecord:
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            await self.runs.lock_lease(connection, context, ticket)
            current = await self._checkpoint(connection, context, ticket.run_id)
            call = await connection.fetchrow(
                _SELECT + " FOR UPDATE OF i,c", context.org_id, ticket.run_id, invocation_id
            )
            if call is None:
                raise CloudDenied("execution call unavailable")
            await self.runs.lock_lease(connection, context, ticket)
            if call["state"] == "settled":
                await self.state.commit_trace_in_transaction(connection, context, ticket, batch)
                return PostgresCallStore.record(call)
            fact = await self._fact(connection, call)
            if fact.receipt is None:
                raise CloudConflict("physical execution unresolved; reservations remain")
            if fact.receipt.outcome == "not_executed":
                if fact.result is not None or fact.receipt.actual_units:
                    raise CloudConflict("nonexecution conflicts with a physical result")
                actual = Usage()
            else:
                if fact.result is None or (
                    fact.result.actual_usage != _usage(call["call_actual_usage"])
                    or fact.receipt.result_digest != canon_result_digest(fact.result)
                    or fact.receipt.actual_units != fact.result.actual_units
                ):
                    raise CloudConflict("physical execution is missing its full result evidence")
                actual = fact.result.actual_usage
            reserved = _subtract(_usage(current["reserved_usage"]), fact.envelope.reservation)
            await self.state.commit_trace_in_transaction(connection, context, ticket, batch)
            await self.runs.lock_lease(connection, context, ticket)
            await connection.execute(
                "UPDATE fh_cloud.runs SET used_units=used_units+$3,"
                "reserved_units=reserved_units-$4 "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
                fact.receipt.actual_units,
                call["reserve_units"],
            )
            await connection.execute(
                "UPDATE fh_cloud.execution_checkpoints SET "
                "used_usage=$3::jsonb,reserved_usage=$4::jsonb "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
                _sum(_usage(current["used_usage"]), actual).model_dump_json(),
                reserved.model_dump_json(),
            )
            await connection.execute(
                "UPDATE fh_cloud.execution_calls SET actual_usage=$4::jsonb "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                context.org_id,
                ticket.run_id,
                invocation_id,
                actual.model_dump_json(),
            )
            row = await connection.fetchrow(
                "UPDATE fh_cloud.invocations SET state='settled',receipt_id=$4 "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 RETURNING *",
                context.org_id,
                ticket.run_id,
                invocation_id,
                fact.receipt.receipt_id,
            )
            await self.runs.emit(
                connection,
                context.org_id,
                ticket.run_id,
                "execution.call_settled",
                {"invocation_id": invocation_id, "actual_units": fact.receipt.actual_units},
            )
            return PostgresCallStore.record(row)

    async def mark_unknown(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        invocation_id: str,
        *,
        batch: tuple[str, ...] = (),
    ) -> CallRecord:
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            await self.runs.lock_lease(connection, context, ticket)
            await self._checkpoint(connection, context, ticket.run_id)
            call = await connection.fetchrow(
                _SELECT + " FOR UPDATE OF i,c", context.org_id, ticket.run_id, invocation_id
            )
            if call is None or call["state"] not in {"sent", "unknown"}:
                raise CloudConflict("execution has no unresolved send")
            await self.state.commit_trace_in_transaction(connection, context, ticket, batch)
            await self.runs.lock_lease(connection, context, ticket)
            row = await connection.fetchrow(
                "UPDATE fh_cloud.invocations SET state='unknown' "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 RETURNING *",
                context.org_id,
                ticket.run_id,
                invocation_id,
            )
            await connection.execute(
                "UPDATE fh_cloud.runs SET status='reconciling' WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
            )
            await self.runs.emit(
                connection,
                context.org_id,
                ticket.run_id,
                "execution.call_unknown",
                {"invocation_id": invocation_id},
                event_id=f"execution-unknown:{ticket.run_id}:{invocation_id}",
            )
            return PostgresCallStore.record(row)

    async def _fact(self, connection: Any, row: Any) -> ExecutionCallFact:
        receipt = await connection.fetchrow(
            "SELECT * FROM fh_cloud.receipts WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
            row["org_id"],
            row["run_id"],
            row["invocation_id"],
        )
        fact = ExecutionCallFact(
            invocation=PostgresCallStore.record(row),
            envelope=CallEnvelope(
                kind=row["kind"],
                logical_id=row["logical_id"],
                canonical_input=_decode(row["canonical_input"]),
                reservation=_usage(row["call_reservation"]),
                reserve_units=row["reserve_units"],
            ),
            idempotency_key=row["idempotency_key"],
            authorization_mode=row["authorization_mode"],
            result=PhysicalResult.model_validate_json(
                _json(_decode(row["canonical_output"])), strict=True
            )
            if row["canonical_output"] is not None
            else None,
            receipt=Receipt.model_validate({key: receipt[key] for key in Receipt.model_fields})
            if receipt is not None
            else None,
        )
        if fact.result is not None and (
            fact.receipt is None
            or fact.receipt.outcome != "completed"
            or fact.receipt.actual_units != fact.result.actual_units
            or fact.receipt.result_digest != canon_result_digest(fact.result)
            or _usage(row["call_actual_usage"]) != fact.result.actual_usage
        ):
            raise CloudConflict("physical result is missing its matching receipt or usage")
        if fact.invocation.state == "settled" and fact.receipt is None:
            raise CloudConflict("settled execution is missing its physical receipt")
        return fact

    async def get(
        self, context: TenantContext, run_id: str, invocation_id: str
    ) -> ExecutionCallFact | None:
        """Current trusted services read facts; absence alone never proves nonexecution."""
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            if not await connection.fetchval(
                "SELECT 1 FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR KEY SHARE",
                context.org_id,
                run_id,
            ):
                return None
            row = await connection.fetchrow(
                _SELECT + " FOR SHARE OF i,c", context.org_id, run_id, invocation_id
            )
            if row is None and await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3)",
                context.org_id,
                run_id,
                invocation_id,
            ):
                raise CloudConflict("invocation is missing its full execution identity")
            return await self._fact(connection, row) if row is not None else None

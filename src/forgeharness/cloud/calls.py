"""Exact approvals, atomic unit reservations and trustworthy receipt settlement.

Only trusted execution services call record_receipt after independently verifying
the provider/tool receipt. A lease or a user-supplied result is never such proof.
This component performs no provider requests and stores digests, not full traces.
"""

from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

from forgeharness.cloud.models import (
    CallRecord,
    CallRequest,
    CloudConflict,
    CloudDenied,
    LeaseTicket,
    Receipt,
    TenantContext,
    approval_fingerprint,
)
from forgeharness.cloud.store import PostgresRunStore


class PostgresCallStore:
    def __init__(self, runs: PostgresRunStore) -> None:
        self.runs = runs

    @staticmethod
    def record(row: Any) -> CallRecord:
        return CallRecord.model_validate({name: row[name] for name in CallRecord.model_fields})

    @staticmethod
    async def _owner_active(connection: Any, org_id: str, owner_id: str) -> None:
        active = await connection.fetchval(
            "SELECT active FROM fh_cloud.members WHERE org_id=$1 AND actor_id=$2 FOR SHARE",
            org_id,
            owner_id,
        )
        if not active:
            raise CloudDenied("run owner is no longer authorized")

    async def grant(
        self,
        context: TenantContext,
        run_id: str,
        call: CallRequest,
        ttl_seconds: float = 300,
    ) -> str:
        if (
            isinstance(ttl_seconds, bool)
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 3600
        ):
            raise ValueError("approval ttl needs 0-3600 finite seconds")
        async with self.runs.transaction(context, "approve") as connection:
            row = await connection.fetchrow(
                "SELECT * FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
                context.org_id,
                run_id,
            )
            if row is None or row["owner_id"] == context.actor_id:
                raise CloudDenied("independent approver required")
            run = self.runs.record(row)
            await self._owner_active(connection, context.org_id, run.owner_id)
            await self.runs.authorize_bound_run(connection, context.org_id, run_id)
            await self.runs.authorize_bound_run(
                connection,
                context.org_id,
                run_id,
                approver_id=context.actor_id,
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            if (
                run.status not in {"queued", "running", "awaiting_approval"}
                or run.cancel_requested
                or run.definition.deadline_at <= now
            ):
                raise CloudConflict("run no longer accepts approval")
            approval_id = f"approval-{uuid4().hex}"
            await connection.execute(
                """
                INSERT INTO fh_cloud.approvals
                (org_id,approval_id,run_id,action_digest,approver_id,expires_at)
                VALUES($1,$2,$3,$4,$5,clock_timestamp()+make_interval(secs=>$6::double precision))
                """,
                context.org_id,
                approval_id,
                run_id,
                approval_fingerprint(run, call),
                context.actor_id,
                float(ttl_seconds),
            )
            await self.runs.emit(
                connection,
                context.org_id,
                run_id,
                "approval.granted",
                {"approval_id": approval_id, "action_digest": approval_fingerprint(run, call)},
            )
            return approval_id

    async def admit(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        call: CallRequest,
        approval_id: str,
    ) -> CallRecord:
        async with self.runs.transaction(context, "execute") as connection:
            run = self.runs.record(await self.runs.lock_lease(connection, context, ticket))
            digest = approval_fingerprint(run, call)
            prior = await connection.fetchrow(
                "SELECT * FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                context.org_id,
                run.run_id,
                call.invocation_id,
            )
            if prior is not None:
                if prior["action_digest"] != digest or prior["approval_id"] != approval_id:
                    raise CloudConflict("invocation id was used for another approved action")
                return self.record(prior)
            await self._owner_active(connection, context.org_id, run.owner_id)
            await self.runs.authorize_bound_run(connection, context.org_id, run.run_id)
            now = await connection.fetchval("SELECT clock_timestamp()")
            if run.status != "running" or run.cancel_requested or run.definition.deadline_at <= now:
                raise CloudConflict("run no longer accepts calls")
            count = await connection.fetchval(
                "SELECT COUNT(*) FROM fh_cloud.invocations WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                run.run_id,
            )
            if (
                count >= run.definition.max_calls
                or run.used_units + run.reserved_units + call.reserve_units
                > run.definition.max_units
            ):
                raise CloudConflict("run budget exhausted")
            grant = await connection.fetchrow(
                "SELECT * FROM fh_cloud.approvals WHERE org_id=$1 AND approval_id=$2 FOR UPDATE",
                context.org_id,
                approval_id,
            )
            if grant is None:
                raise CloudDenied("approval unavailable")
            approver = await connection.fetchrow(
                "SELECT active,can_approve FROM fh_cloud.members "
                "WHERE org_id=$1 AND actor_id=$2 FOR SHARE",
                context.org_id,
                grant["approver_id"],
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            if (
                grant["run_id"] != run.run_id
                or grant["action_digest"] != digest
                or grant["consumed"]
                or grant["expires_at"] <= now
                or grant["approver_id"] == run.owner_id
                or approver is None
                or not approver["active"]
                or not approver["can_approve"]
            ):
                raise CloudDenied("approval is no longer valid for this action")
            locked = await self.runs.lock_lease(connection, context, ticket)
            if run.definition.deadline_at <= locked["db_now"]:
                raise CloudConflict("run deadline passed while awaiting approval locks")
            await connection.execute(
                "UPDATE fh_cloud.approvals SET consumed=true WHERE org_id=$1 AND approval_id=$2",
                context.org_id,
                approval_id,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO fh_cloud.invocations
                (org_id,run_id,invocation_id,tool_name,effect_class,arguments_digest,
                 action_digest,approval_id,reserve_units,state,admission_epoch)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'admitted',$10) RETURNING *
                """,
                context.org_id,
                run.run_id,
                call.invocation_id,
                call.tool_name,
                call.effect_class,
                call.arguments_digest,
                digest,
                approval_id,
                call.reserve_units,
                ticket.epoch,
            )
            await connection.execute(
                "UPDATE fh_cloud.runs SET reserved_units=reserved_units+$3 "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                run.run_id,
                call.reserve_units,
            )
            await self.runs.emit(
                connection,
                context.org_id,
                run.run_id,
                "call.admitted",
                {"invocation_id": call.invocation_id, "reserve_units": call.reserve_units},
            )
            return self.record(row)

    async def mark_sent(
        self, context: TenantContext, ticket: LeaseTicket, invocation_id: str
    ) -> CallRecord:
        """Commit the send-intent before any external request; never retry this on timeout."""
        async with self.runs.transaction(context, "execute") as connection:
            run = self.runs.record(await self.runs.lock_lease(connection, context, ticket))
            await self._owner_active(connection, context.org_id, run.owner_id)
            await self.runs.authorize_bound_run(connection, context.org_id, run.run_id)
            now = await connection.fetchval("SELECT clock_timestamp()")
            if run.status != "running" or run.cancel_requested or run.definition.deadline_at <= now:
                raise CloudConflict("run no longer accepts sending")
            invocation = await connection.fetchrow(
                "SELECT state,admission_epoch FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 FOR UPDATE",
                context.org_id,
                run.run_id,
                invocation_id,
            )
            # Serialize with receipt ingestion. A pre-lock snapshot could miss a
            # nonexecution receipt committed while this operation waits.
            has_receipt = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.receipts "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3)",
                context.org_id,
                run.run_id,
                invocation_id,
            )
            if (
                invocation is None
                or invocation["state"] != "admitted"
                or invocation["admission_epoch"] != ticket.epoch
                or has_receipt
            ):
                raise CloudConflict("call cannot be sent or already has a final receipt")
            approval = await connection.fetchrow(
                """
                SELECT a.consumed, a.expires_at, m.active, m.can_approve
                FROM fh_cloud.invocations i
                JOIN fh_cloud.approvals a USING (org_id,approval_id)
                JOIN fh_cloud.members m ON m.org_id=a.org_id AND m.actor_id=a.approver_id
                WHERE i.org_id=$1 AND i.run_id=$2 AND i.invocation_id=$3
                    AND i.action_digest=a.action_digest AND a.run_id=i.run_id
                FOR SHARE OF a,m
                """,
                context.org_id,
                run.run_id,
                invocation_id,
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            if (
                approval is None
                or not approval["consumed"]
                or not approval["active"]
                or not approval["can_approve"]
                or approval["expires_at"] <= now
            ):
                raise CloudDenied("approval is no longer valid for sending")
            locked = await self.runs.lock_lease(connection, context, ticket)
            if run.definition.deadline_at <= locked["db_now"]:
                raise CloudConflict("run deadline passed while awaiting sending locks")
            if locked["used_units"] + locked["reserved_units"] > run.definition.max_units:
                raise CloudConflict("actual usage exhausted the run budget before sending")
            row = await connection.fetchrow(
                """
                UPDATE fh_cloud.invocations SET state='sent'
                WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3
                    AND state='admitted' AND admission_epoch=$4 RETURNING *
                """,
                context.org_id,
                run.run_id,
                invocation_id,
                ticket.epoch,
            )
            if row is None:
                raise CloudConflict("call cannot be sent or may already have been sent")
            await self.runs.emit(
                connection,
                context.org_id,
                run.run_id,
                "call.sent",
                {"invocation_id": invocation_id},
            )
            return self.record(row)

    async def mark_unknown(
        self, context: TenantContext, ticket: LeaseTicket, invocation_id: str
    ) -> CallRecord:
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            await self.runs.lock_lease(connection, context, ticket)
            row = await connection.fetchrow(
                """
                UPDATE fh_cloud.invocations SET state='unknown'
                WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 AND state IN ('sent','unknown')
                RETURNING *
                """,
                context.org_id,
                ticket.run_id,
                invocation_id,
            )
            if row is None:
                raise CloudConflict("call has no unresolved send")
            await self.runs.lock_lease(connection, context, ticket)
            await connection.execute(
                "UPDATE fh_cloud.runs SET status='reconciling' WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
            )
            await self.runs.emit(
                connection,
                context.org_id,
                ticket.run_id,
                "call.unknown",
                {"invocation_id": invocation_id},
                event_id=f"unknown:{ticket.run_id}:{invocation_id}",
            )
            return self.record(row)

    async def record_receipt(
        self, context: TenantContext, run_id: str, invocation_id: str, receipt: Receipt
    ) -> None:
        """Append a trusted provider fact even after its original worker lost ownership.

        A current execution-service identity is required. Caller must authenticate
        the receipt with the provider/tool; this API is never exposed to tenants.
        It never advances a run or releases any reservation by itself.
        """
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            # Match projection operations' run -> invocation lock order. The
            # outbox FK also needs a run key-share lock; acquiring it only after
            # invocation would deadlock with a sender/settler holding the run.
            found = await connection.fetchval(
                "SELECT 1 FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR KEY SHARE",
                context.org_id,
                run_id,
            )
            if found is None:
                raise CloudDenied("invocation unavailable")
            call = await connection.fetchrow(
                "SELECT state FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 FOR UPDATE",
                context.org_id,
                run_id,
                invocation_id,
            )
            if call is None:
                raise CloudDenied("invocation unavailable")
            if call["state"] == "admitted" and receipt.outcome != "not_executed":
                raise CloudConflict("unadmitted physical execution cannot report success")
            await connection.execute(
                """
                INSERT INTO fh_cloud.receipts
                (org_id,run_id,invocation_id,receipt_id,actual_units,result_digest,outcome)
                VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING
                """,
                context.org_id,
                run_id,
                invocation_id,
                receipt.receipt_id,
                receipt.actual_units,
                receipt.result_digest,
                receipt.outcome,
            )
            stored = await connection.fetchrow(
                "SELECT * FROM fh_cloud.receipts "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                context.org_id,
                run_id,
                invocation_id,
            )
            if stored is None or any(stored[k] != v for k, v in receipt.model_dump().items()):
                raise CloudConflict("receipt conflicts with recorded provider facts")
            await self.runs.emit(
                connection,
                context.org_id,
                run_id,
                "receipt.recorded",
                {"invocation_id": invocation_id, **receipt.model_dump()},
                event_id=f"receipt:{receipt.receipt_id}",
            )

    async def settle(
        self, context: TenantContext, ticket: LeaseTicket, invocation_id: str
    ) -> CallRecord:
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            await self.runs.lock_lease(connection, context, ticket)
            call = await connection.fetchrow(
                "SELECT * FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 FOR UPDATE",
                context.org_id,
                ticket.run_id,
                invocation_id,
            )
            if call is None:
                raise CloudDenied("invocation unavailable")
            await self.runs.lock_lease(connection, context, ticket)
            if call["state"] == "settled":
                return self.record(call)
            receipt = await connection.fetchrow(
                "SELECT * FROM fh_cloud.receipts "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3",
                context.org_id,
                ticket.run_id,
                invocation_id,
            )
            if receipt is None:
                raise CloudConflict("provider result is unresolved; reservation remains")
            await connection.execute(
                """
                UPDATE fh_cloud.runs SET used_units=used_units+$3,
                    reserved_units=reserved_units-$4 WHERE org_id=$1 AND run_id=$2
                """,
                context.org_id,
                ticket.run_id,
                receipt["actual_units"],
                call["reserve_units"],
            )
            result = await connection.fetchrow(
                "UPDATE fh_cloud.invocations SET state='settled',receipt_id=$4 "
                "WHERE org_id=$1 AND run_id=$2 AND invocation_id=$3 RETURNING *",
                context.org_id,
                ticket.run_id,
                invocation_id,
                receipt["receipt_id"],
            )
            await self.runs.emit(
                connection,
                context.org_id,
                ticket.run_id,
                "call.settled",
                {"invocation_id": invocation_id, "actual_units": receipt["actual_units"]},
            )
            return self.record(result)

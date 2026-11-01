"""PostgreSQL run admission, current authorization and lease-fenced projections.

This is an optional Cloud state component, not a replacement for the SDK's local
SQLite controllers. Hosts authenticate TenantContext before calling it. The
runtime role is deliberately unable to own or migrate its state tables.
"""

from __future__ import annotations

import importlib
import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, cast
from uuid import uuid4

from forgeharness.cloud.models import (
    CloudConflict,
    CloudDenied,
    LeaseTicket,
    RunDefinition,
    RunRecord,
    StaleLease,
    TenantContext,
)

_ACTIVE = frozenset({"queued", "running", "awaiting_approval", "reconciling"})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "exhausted"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _ttl(seconds: float) -> float:
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError("lease ttl must be finite, positive and at most 300 seconds")
    return float(seconds)


class PostgresRunStore:
    """Operation-level transactions over a separately provisioned Cloud schema."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> PostgresRunStore:
        try:
            asyncpg = cast(Any, importlib.import_module("asyncpg"))
        except ImportError as exc:
            raise RuntimeError("Cloud PostgreSQL requires the cloud extra") from exc
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=30)
        try:
            async with pool.acquire() as connection:
                await cls._runtime_role(connection)
                if await connection.fetchval("SELECT version FROM fh_cloud.schema_version") not in (
                    2,
                    3,
                ):
                    raise CloudConflict("unsupported Cloud state schema")
        except BaseException:
            await pool.close()
            raise
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    @staticmethod
    async def authorize_bound_run(
        connection: Any,
        org_id: str,
        run_id: str,
        *,
        approver_id: str | None = None,
    ) -> None:
        """Revalidate a platform owner's durable binding before a new send.

        Schema-2/legacy runs have no platform binding and preserve their original
        trusted-host contract. Settlement paths intentionally do not call this.
        """
        if await connection.fetchval("SELECT version FROM fh_cloud.schema_version") < 3:
            return
        binding = await connection.fetchrow(
            "SELECT b.*,r.owner_id,r.definition FROM fh_cloud.run_bindings b "
            "JOIN fh_cloud.runs r USING(org_id,run_id) WHERE b.org_id=$1 AND b.run_id=$2",
            org_id,
            run_id,
        )
        if binding is None:
            return
        from forgeharness.cloud.identity import authorize_deployment

        deployment = await authorize_deployment(
            connection,
            org_id,
            approver_id or binding["owner_id"],
            binding["deployment_id"],
            workspace_id=binding["workspace_id"],
            permission="approve" if approver_id else "use",
        )
        definition = binding["definition"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        if (
            deployment["binding_digest"] != binding["binding_digest"]
            or definition["binding_digest"] != binding["binding_digest"]
            or await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.approval_decisions "
                "WHERE org_id=$1 AND run_id=$2 AND decision='reject')",
                org_id,
                run_id,
            )
        ):
            raise CloudDenied("run authorization unavailable")

    @staticmethod
    async def _runtime_role(connection: Any) -> None:
        unsafe = await connection.fetchval(
            """
            SELECT current_user <> session_user OR EXISTS (
                SELECT 1 FROM pg_roles r
                WHERE (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole)
                  AND pg_has_role(current_user, r.oid, 'MEMBER')
            ) OR EXISTS (
                SELECT 1 FROM pg_namespace n
                WHERE n.nspname='fh_cloud'
                  AND pg_has_role(current_user, n.nspowner, 'MEMBER')
            ) OR EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='fh_cloud'
                  AND pg_has_role(current_user, c.relowner, 'MEMBER')
            )
            """
        )
        if unsafe:
            raise CloudDenied("runtime role must not bypass isolation or own Cloud state")

    @asynccontextmanager
    async def transaction(
        self,
        context: TenantContext,
        permission: Literal["member", "execute", "approve"] = "member",
        *,
        allow_inactive_org: bool = False,
    ) -> AsyncIterator[Any]:
        """Bind one transaction's tenant; authorization never comes from cached roles."""
        if permission not in {"member", "execute", "approve"}:
            raise ValueError("unknown Cloud permission")
        async with self.pool.acquire() as connection, connection.transaction():
            await self._runtime_role(connection)
            await connection.execute(
                "SELECT set_config('fh.org_id', $1, true), set_config('fh.actor_id', $2, true)",
                context.org_id,
                context.actor_id,
            )
            member = await connection.fetchrow(
                """
                SELECT m.active, m.can_execute, m.can_approve, o.active AS org_active
                FROM fh_cloud.members m JOIN fh_cloud.organizations o USING(org_id)
                WHERE m.org_id=$1 AND m.actor_id=$2
                FOR SHARE OF m, o
                """,
                context.org_id,
                context.actor_id,
            )
            if (
                member is None
                or not member["active"]
                or (not member["org_active"] and not allow_inactive_org)
                or (permission == "execute" and not member["can_execute"])
                or (permission == "approve" and not member["can_approve"])
            ):
                raise CloudDenied("Cloud operation unavailable or unauthorized")
            yield connection

    @staticmethod
    def record(row: Any) -> RunRecord:
        definition = row["definition"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        return RunRecord(
            org_id=row["org_id"],
            run_id=row["run_id"],
            owner_id=row["owner_id"],
            request_key=row["request_key"],
            definition=RunDefinition.model_validate(definition),
            status=row["status"],
            used_units=row["used_units"],
            reserved_units=row["reserved_units"],
            epoch=row["epoch"],
            worker_id=row["worker_id"],
            lease_actor_id=row["lease_actor_id"],
            lease_until=row["lease_until"],
            cancel_requested=row["cancel_requested"],
        )

    @staticmethod
    async def emit(
        connection: Any,
        org_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> str:
        """Append a transactional event; reusing an id requires identical content."""
        event_id = event_id or f"event-{uuid4().hex}"
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        row = await connection.fetchrow(
            """
            INSERT INTO fh_cloud.outbox(org_id,event_id,run_id,kind,payload)
            VALUES($1,$2,$3,$4,$5::jsonb)
            ON CONFLICT(org_id,event_id) DO NOTHING RETURNING event_id
            """,
            org_id,
            event_id,
            run_id,
            kind,
            encoded,
        )
        if row is None:
            previous = await connection.fetchrow(
                "SELECT run_id,kind,payload FROM fh_cloud.outbox WHERE org_id=$1 AND event_id=$2",
                org_id,
                event_id,
            )
            stored = previous["payload"] if previous is not None else None
            if isinstance(stored, str):
                stored = json.loads(stored)
            if (
                previous is None
                or previous["run_id"] != run_id
                or previous["kind"] != kind
                or stored != payload
            ):
                raise CloudConflict("event id was used for different content")
        return event_id

    async def submit(
        self, context: TenantContext, request_key: str, definition: RunDefinition
    ) -> RunRecord:
        if (
            not isinstance(request_key, str)
            or not 1 <= len(request_key) <= 200
            or "\x00" in request_key
        ):
            raise ValueError("request key needs 1-200 non-NUL characters")
        async with self.transaction(context) as connection:
            result, _ = await self.submit_in_transaction(
                connection, context, request_key, definition
            )
            return result

    async def submit_in_transaction(
        self,
        connection: Any,
        context: TenantContext,
        request_key: str,
        definition: RunDefinition,
        *,
        accept_existing_deadline: bool = False,
    ) -> tuple[RunRecord, bool]:
        """Trusted composition point; caller owns this tenant's authorized transaction."""
        if (
            not isinstance(request_key, str)
            or not 1 <= len(request_key) <= 200
            or "\x00" in request_key
        ):
            raise ValueError("request key needs 1-200 non-NUL characters")
        row = await connection.fetchrow(
            """
                INSERT INTO fh_cloud.runs(org_id,run_id,owner_id,request_key,definition)
                SELECT $1,$2,$3,$4,$5::jsonb WHERE $6::timestamptz > clock_timestamp()
                ON CONFLICT(org_id,owner_id,request_key) DO NOTHING RETURNING *
                """,
            context.org_id,
            f"run-{uuid4().hex}",
            context.actor_id,
            request_key,
            definition.model_dump_json(),
            definition.deadline_at,
        )
        created = row is not None
        if created:
            await self.emit(
                connection,
                context.org_id,
                row["run_id"],
                "run.queued",
                {"run_id": row["run_id"]},
            )
        else:
            row = await connection.fetchrow(
                "SELECT * FROM fh_cloud.runs WHERE org_id=$1 AND owner_id=$2 AND request_key=$3",
                context.org_id,
                context.actor_id,
                request_key,
            )
            if row is None:
                raise CloudConflict("run deadline has already expired")
        result = self.record(row)
        expected = definition
        if not created and accept_existing_deadline:
            expected = definition.model_copy(update={"deadline_at": result.definition.deadline_at})
        if result.definition != expected:
            raise CloudConflict("request key was used for a different run definition")
        return result, created

    @staticmethod
    async def _visible_run(connection: Any, context: TenantContext, run_id: str) -> Any:
        row = await connection.fetchrow(
            """
            SELECT r.* FROM fh_cloud.runs r
            WHERE r.org_id=$1 AND r.run_id=$2 AND (
                r.owner_id=$3 OR EXISTS (
                    SELECT 1 FROM fh_cloud.members m
                    WHERE m.org_id=$1 AND m.actor_id=$3 AND m.active AND m.can_execute
                )
            )
            """,
            context.org_id,
            run_id,
            context.actor_id,
        )
        if row is None:
            raise CloudDenied("Cloud operation unavailable or unauthorized")
        return row

    async def get(self, context: TenantContext, run_id: str) -> RunRecord:
        async with self.transaction(context) as connection:
            return self.record(await self._visible_run(connection, context, run_id))

    async def claim(
        self,
        context: TenantContext,
        run_id: str,
        worker_id: str,
        ttl_seconds: float = 30,
    ) -> LeaseTicket:
        seconds = _ttl(ttl_seconds)
        if not _IDENTIFIER.fullmatch(worker_id):
            raise ValueError("worker id needs 1-128 identifier characters")
        async with self.transaction(context, "execute", allow_inactive_org=True) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
                context.org_id,
                run_id,
            )
            if row is None:
                raise CloudDenied("Cloud operation unavailable or unauthorized")
            row = dict(row)
            row["db_now"] = await connection.fetchval("SELECT clock_timestamp()")
            if row["status"] not in _ACTIVE:
                raise CloudConflict("run is already settled")
            if row["lease_until"] is not None and row["lease_until"] > row["db_now"]:
                raise CloudConflict("run already has an active lease")
            owner_active = await connection.fetchval(
                """
                SELECT m.active AND o.active FROM fh_cloud.members m
                JOIN fh_cloud.organizations o USING(org_id)
                WHERE m.org_id=$1 AND m.actor_id=$2
                """,
                context.org_id,
                row["owner_id"],
            )
            unresolved = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND state<>'settled')",
                context.org_id,
                run_id,
            )
            definition = self.record(row).definition
            reconcile = (
                not owner_active
                or row["cancel_requested"]
                or unresolved
                or definition.deadline_at <= row["db_now"]
            )
            claimed = await connection.fetchrow(
                """
                UPDATE fh_cloud.runs SET epoch=epoch+1, worker_id=$3, lease_actor_id=$4,
                    lease_until=clock_timestamp()+make_interval(secs=>$5::double precision),
                    status=$6
                WHERE org_id=$1 AND run_id=$2 RETURNING *
                """,
                context.org_id,
                run_id,
                worker_id,
                context.actor_id,
                seconds,
                "reconciling" if reconcile else "running",
            )
            await self.emit(
                connection,
                context.org_id,
                run_id,
                "run.claimed",
                {"epoch": claimed["epoch"], "worker_id": worker_id, "status": claimed["status"]},
            )
            return self._ticket(claimed)

    @staticmethod
    def _ticket(row: Any) -> LeaseTicket:
        return LeaseTicket(
            org_id=row["org_id"],
            run_id=row["run_id"],
            actor_id=row["lease_actor_id"],
            worker_id=row["worker_id"],
            epoch=row["epoch"],
            expires_at=row["lease_until"],
        )

    @staticmethod
    async def lock_lease(connection: Any, context: TenantContext, ticket: LeaseTicket) -> Any:
        if ticket.org_id != context.org_id or ticket.actor_id != context.actor_id:
            raise StaleLease("execution lease is not current")
        row = await connection.fetchrow(
            "SELECT * FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
            context.org_id,
            ticket.run_id,
        )
        if row is not None:
            row = dict(row)
            # Evaluate expiry after waiting for the row lock, not before it.
            row["db_now"] = await connection.fetchval("SELECT clock_timestamp()")
        if (
            row is None
            or row["epoch"] != ticket.epoch
            or row["worker_id"] != ticket.worker_id
            or row["lease_actor_id"] != context.actor_id
            or row["lease_until"] is None
            or row["lease_until"] <= row["db_now"]
            or row["status"] not in _ACTIVE
        ):
            raise StaleLease("execution lease is not current")
        return row

    async def renew(
        self, context: TenantContext, ticket: LeaseTicket, ttl_seconds: float = 30
    ) -> LeaseTicket:
        seconds = _ttl(ttl_seconds)
        async with self.transaction(context, "execute", allow_inactive_org=True) as connection:
            await self.lock_lease(connection, context, ticket)
            row = await connection.fetchrow(
                "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()"
                "+make_interval(secs=>$3::double precision) "
                "WHERE org_id=$1 AND run_id=$2 RETURNING *",
                context.org_id,
                ticket.run_id,
                seconds,
            )
            return self._ticket(row)

    async def release(
        self, context: TenantContext, ticket: LeaseTicket, ttl_seconds: float = 30
    ) -> None:
        """Give the lease up now instead of renewing it again.

        A worker that stops reporting progress uses this so another worker can
        reconcile the run without waiting out the last renewal. The row keeps the
        same epoch and worker id, so the released worker's own next fenced write
        fails on :meth:`lock_lease` rather than silently continuing.
        """
        seconds = _ttl(ttl_seconds)
        async with self.transaction(context, "execute", allow_inactive_org=True) as connection:
            await self.lock_lease(connection, context, ticket)
            await connection.execute(
                "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()"
                "-make_interval(secs=>$3::double precision) "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                ticket.run_id,
                seconds,
            )

    async def cancel(self, context: TenantContext, run_id: str) -> RunRecord:
        async with self.transaction(context) as connection:
            await self._visible_run(connection, context, run_id)
            row = await connection.fetchrow(
                "SELECT * FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
                context.org_id,
                run_id,
            )
            if row["status"] in _TERMINAL:
                raise CloudConflict("a settled run cannot be cancelled")
            if row["cancel_requested"]:
                return self.record(row)
            updated = await connection.fetchrow(
                "UPDATE fh_cloud.runs SET cancel_requested=TRUE "
                "WHERE org_id=$1 AND run_id=$2 RETURNING *",
                context.org_id,
                run_id,
            )
            await self.emit(connection, context.org_id, run_id, "run.cancel_requested", {})
            return self.record(updated)

    async def finish(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        status: Literal["succeeded", "failed", "cancelled", "exhausted"] = "succeeded",
    ) -> RunRecord:
        if status not in _TERMINAL:
            raise ValueError("run finish needs a terminal status")
        async with self.transaction(context, "execute", allow_inactive_org=True) as connection:
            row = await self.lock_lease(connection, context, ticket)
            managed = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.execution_checkpoints "
                "WHERE org_id=$1 AND run_id=$2)",
                context.org_id,
                ticket.run_id,
            )
            if managed:
                raise CloudConflict(
                    "managed execution must finish with its checkpoint and evidence"
                )
            unresolved = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND state<>'settled')",
                context.org_id,
                ticket.run_id,
            )
            if unresolved or row["reserved_units"]:
                raise CloudConflict("unresolved calls or reservations prevent run completion")
            if status == "succeeded":
                confirmed = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM fh_cloud.invocations i JOIN fh_cloud.receipts r
                        USING(org_id,run_id,invocation_id)
                        WHERE i.org_id=$1 AND i.run_id=$2 AND i.state='settled'
                          AND i.receipt_id=r.receipt_id AND r.outcome='completed'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM fh_cloud.invocations i
                        LEFT JOIN fh_cloud.receipts r USING(org_id,run_id,invocation_id)
                        WHERE i.org_id=$1 AND i.run_id=$2 AND (
                            r.receipt_id IS NULL OR i.receipt_id<>r.receipt_id
                            OR r.outcome<>'completed'
                        )
                    )
                    """,
                    context.org_id,
                    ticket.run_id,
                )
                if not confirmed:
                    raise CloudConflict("success requires a confirmed settled invocation")
                if row["used_units"] > self.record(row).definition.max_units:
                    raise CloudConflict("exceeded run budget cannot report success")
            updated = await connection.fetchrow(
                "UPDATE fh_cloud.runs SET status=$3, lease_until=NULL "
                "WHERE org_id=$1 AND run_id=$2 RETURNING *",
                context.org_id,
                ticket.run_id,
                status,
            )
            await self.emit(
                connection, context.org_id, ticket.run_id, "run.finished", {"status": status}
            )
            return self.record(updated)

    async def events(self, context: TenantContext, run_id: str) -> tuple[dict[str, Any], ...]:
        async with self.transaction(context) as connection:
            await self._visible_run(connection, context, run_id)
            rows = await connection.fetch(
                "SELECT event_id,kind,payload,created_at,delivered_at FROM fh_cloud.outbox "
                "WHERE org_id=$1 AND run_id=$2 ORDER BY created_at,event_id",
                context.org_id,
                run_id,
            )
            result = []
            for row in rows:
                event = dict(row)
                if isinstance(event["payload"], str):
                    event["payload"] = json.loads(event["payload"])
                result.append(event)
            return tuple(result)

"""Application-neutral, scoped control service for the invitation Alpha.

Public adapters pass a freshly authenticated Principal, never a TenantContext
decoded from HTTP. All permission decisions are re-read under database locks.
Only minimal DTOs leave this module. Full checkpoints/evidence remain internal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

from forgeharness.cloud.execution import governance_request, tool_envelope
from forgeharness.cloud.hosted import HostedApplication
from forgeharness.cloud.models import (
    CloudConflict,
    CloudDenied,
    RunRecord,
    TenantContext,
    approval_fingerprint,
)
from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.cloud.store import PostgresRunStore
from forgeharness.domain.models import RunResult, RunStatus


class CurrentPrincipal(Protocol):
    @property
    def account_id(self) -> str: ...

    @property
    def org_id(self) -> str | None: ...


@dataclass(frozen=True)
class AdmissionLimits:
    user_per_minute: int = 10
    org_per_minute: int = 30
    user_pending: int = 5
    org_pending: int = 20
    org_total: int = 1000

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("admission limits must be positive integers")
        if self.org_pending > 100:
            raise ValueError("serial scanner supports at most 100 pending organization runs")


def _object(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PlatformRuntime(HostedRuntime):
    def __init__(
        self,
        store: PostgresRunStore,
        registry: Mapping[str, HostedApplication],
        *,
        limits: AdmissionLimits | None = None,
    ) -> None:
        super().__init__(store, registry)
        self.limits = limits or AdmissionLimits()

    @staticmethod
    def _context(principal: CurrentPrincipal, org_id: str) -> TenantContext:
        if principal.org_id != org_id:
            raise CloudDenied("object unavailable")
        return TenantContext(org_id=org_id, actor_id=principal.account_id)

    def registered_deployment(self, deployment_id: str) -> dict[str, str]:
        app = self.registry.get(deployment_id)
        if app is None:
            raise CloudDenied("deployment unavailable")
        return {"application_id": app.application.id, "binding_digest": app.binding_digest}

    @staticmethod
    async def _authorize(
        connection: Any,
        context: TenantContext,
        workspace_id: str,
        deployment_id: str,
        *,
        permission: Literal["use", "approve"] = "use",
        historical: bool = False,
    ) -> Any:
        from forgeharness.cloud.identity import authorize_deployment

        return await authorize_deployment(
            connection,
            context.org_id,
            context.actor_id,
            deployment_id,
            workspace_id=workspace_id,
            permission=permission,
            historical=historical,
        )

    async def _quota(self, connection: Any, context: TenantContext, request_key: str) -> None:
        # The organization lock serializes submissions without introducing a
        # paid queue or a second budget authority. A retry consumes no new slot.
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 130))", context.org_id
        )
        if await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM fh_cloud.runs "
            "WHERE org_id=$1 AND owner_id=$2 AND request_key=$3)",
            context.org_id,
            context.actor_id,
            request_key,
        ):
            return
        counts = await connection.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER(WHERE status IN "
            "('queued','running','awaiting_approval','reconciling')) AS pending, "
            "count(*) FILTER(WHERE owner_id=$2 AND status IN "
            "('queued','running','awaiting_approval','reconciling')) AS user_pending, "
            "count(*) FILTER(WHERE created_at>clock_timestamp()-interval '60 seconds') AS rate, "
            "count(*) FILTER(WHERE owner_id=$2 AND "
            "created_at>clock_timestamp()-interval '60 seconds') AS user_rate "
            "FROM fh_cloud.runs WHERE org_id=$1",
            context.org_id,
            context.actor_id,
        )
        limits = self.limits
        if any(
            (
                counts["total"] >= limits.org_total,
                counts["pending"] >= limits.org_pending,
                counts["user_pending"] >= limits.user_pending,
                counts["rate"] >= limits.org_per_minute,
                counts["user_rate"] >= limits.user_per_minute,
            )
        ):
            raise CloudConflict("submission limit reached")

    async def accept(  # type: ignore[override]
        self,
        principal: CurrentPrincipal,
        org_id: str,
        workspace_id: str,
        deployment_id: str,
        task: str,
        request_key: str,
    ) -> dict[str, Any]:
        context = self._context(principal, org_id)
        registered = self.registry.get(deployment_id)
        if registered is None:
            raise CloudDenied("deployment unavailable")

        async def guard(connection: Any) -> None:
            await self._quota(connection, context, request_key)
            deployment = await self._authorize(connection, context, workspace_id, deployment_id)
            if deployment["binding_digest"] != registered.binding_digest:
                raise CloudDenied("deployment unavailable")

        async def bind(connection: Any, run: RunRecord, created: bool) -> None:
            scope = list(registered.resources)
            if created:
                await connection.execute(
                    "INSERT INTO fh_cloud.run_bindings "
                    "(org_id,run_id,workspace_id,deployment_id,binding_digest,scope) "
                    "VALUES($1,$2,$3,$4,$5,$6::jsonb)",
                    org_id,
                    run.run_id,
                    workspace_id,
                    deployment_id,
                    registered.binding_digest,
                    json.dumps(scope),
                )
            else:
                binding = await connection.fetchrow(
                    "SELECT * FROM fh_cloud.run_bindings WHERE org_id=$1 AND run_id=$2",
                    org_id,
                    run.run_id,
                )
                if binding is None or (
                    binding["workspace_id"] != workspace_id
                    or binding["deployment_id"] != deployment_id
                    or binding["binding_digest"] != registered.binding_digest
                    or _object(binding["scope"]) != scope
                ):
                    raise CloudConflict("request key conflicts with the accepted binding")

        # Explicitly call the internal host method: no request path can inject
        # either callback, scope, budget or the execution identity.
        run = await self._accept_record(
            context,
            deployment_id,
            request_key,
            task,
            session_id="platform-session",
            admission_guard=guard,
            bind_run=bind,
        )
        # Return the committed acceptance receipt, even if a revocation wins
        # immediately afterward. A second protected read is independently gated.
        return {
            "run_id": run.run_id,
            "workspace_id": workspace_id,
            "deployment_id": deployment_id,
            "status": run.status,
            "cancel_requested": run.cancel_requested,
            "result": None,
            "used_units": run.used_units,
            "reserved_units": run.reserved_units,
            "error": None,
        }

    async def _bound(
        self,
        connection: Any,
        context: TenantContext,
        workspace_id: str,
        run_id: str,
        *,
        approval: bool = False,
        lock: bool = False,
    ) -> Any:
        row = await connection.fetchrow(
            "SELECT r.*,b.workspace_id,b.deployment_id,b.binding_digest AS accepted_binding "
            "FROM fh_cloud.runs r "
            "JOIN fh_cloud.run_bindings b USING(org_id,run_id) "
            "WHERE r.org_id=$1 AND r.run_id=$2 AND b.workspace_id=$3"
            + (" FOR UPDATE OF r" if lock else " FOR SHARE OF r"),
            context.org_id,
            run_id,
            workspace_id,
        )
        if row is None or (not approval and row["owner_id"] != context.actor_id):
            raise CloudDenied("run unavailable")
        # READ COMMITTED takes its statement snapshot before a row-lock wait.
        # Joining the checkpoint into that statement can combine the refreshed
        # locked run with a superseded proposal. Read it only after the run lock
        # is held; every execution checkpoint writer takes this same run lock.
        checkpoint = await connection.fetchval(
            "SELECT checkpoint FROM fh_cloud.execution_checkpoints WHERE org_id=$1 AND run_id=$2",
            context.org_id,
            run_id,
        )
        if checkpoint is None:
            raise CloudDenied("run unavailable")
        await self._authorize(
            connection,
            context,
            workspace_id,
            row["deployment_id"],
            permission="approve" if approval else "use",
            historical=True,
        )
        return dict(row) | {"checkpoint": checkpoint}

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        checkpoint = _object(row["checkpoint"])
        error = None
        if row["status"] == "cancelled":
            error = "cancelled"
        elif row["status"] in {"failed", "exhausted"}:
            error = "execution_failed" if row["status"] == "failed" else "budget_exhausted"
        return {
            "run_id": row["run_id"],
            "workspace_id": row["workspace_id"],
            "deployment_id": row["deployment_id"],
            "status": row["status"],
            "cancel_requested": row["cancel_requested"],
            "result": checkpoint.get("final_output") if row["status"] == "succeeded" else None,
            "used_units": row["used_units"],
            "reserved_units": row["reserved_units"],
            "error": error,
        }

    async def inspect(  # type: ignore[override]
        self, principal: CurrentPrincipal, org_id: str, workspace_id: str, run_id: str
    ) -> dict[str, Any]:
        context = self._context(principal, org_id)
        async with self.store.transaction(context) as connection:
            row = await self._bound(connection, context, workspace_id, run_id)
            dto = self._public(row)
            if (
                await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM fh_cloud.approval_decisions "
                    "WHERE org_id=$1 AND run_id=$2 AND decision='reject')",
                    org_id,
                    run_id,
                )
                and row["status"] == "cancelled"
            ):
                dto["error"] = "approval_rejected"
            return dto

    @staticmethod
    def _page(limit: int, cursor: str | None) -> str:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("page limit must be 1-100")
        if cursor is not None and (len(cursor) > 128 or not cursor.startswith("run-")):
            raise ValueError("invalid page cursor")
        return cursor or ""

    async def list_runs(
        self,
        principal: CurrentPrincipal,
        org_id: str,
        workspace_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        context = self._context(principal, org_id)
        after = self._page(limit, cursor)
        async with self.store.transaction(context) as connection:
            rows = await connection.fetch(
                "SELECT r.run_id FROM fh_cloud.runs r "
                "JOIN fh_cloud.run_bindings b USING(org_id,run_id) "
                "JOIN fh_cloud.deployment_grants g ON g.org_id=b.org_id "
                "AND g.deployment_id=b.deployment_id AND g.account_id=r.owner_id "
                "WHERE r.org_id=$1 AND r.owner_id=$2 AND b.workspace_id=$3 AND r.run_id>$4 "
                "AND g.active AND g.can_use ORDER BY r.run_id LIMIT $5",
                org_id,
                context.actor_id,
                workspace_id,
                after,
                limit + 1,
            )
            items = []
            for row in rows[:limit]:
                items.append(
                    self._public(
                        await self._bound(connection, context, workspace_id, row["run_id"])
                    )
                )
            return {
                "items": items,
                "next_cursor": (rows[limit - 1]["run_id"] if len(rows) > limit else None),
            }

    async def cancel(  # type: ignore[override]
        self, principal: CurrentPrincipal, org_id: str, workspace_id: str, run_id: str
    ) -> dict[str, Any]:
        context = self._context(principal, org_id)
        async with self.store.transaction(context) as connection:
            row = await self._bound(connection, context, workspace_id, run_id, lock=True)
            if row["status"] in {"succeeded", "failed", "cancelled", "exhausted"}:
                raise CloudConflict("run is already settled")
            await connection.execute(
                "UPDATE fh_cloud.runs SET cancel_requested=true WHERE org_id=$1 AND run_id=$2",
                org_id,
                run_id,
            )
            await self.store.emit(
                connection,
                org_id,
                run_id,
                "run.cancel_requested",
                {},
                event_id="cancel:" + run_id,
            )
        return await self.inspect(principal, org_id, workspace_id, run_id)

    async def _proposal(self, connection: Any, context: TenantContext, row: Any) -> dict[str, Any]:
        previous = RunResult.model_validate(_object(row["checkpoint"]))
        run = self.store.record(row)
        if (
            row["status"] != "awaiting_approval"
            or row["cancel_requested"]
            or previous.status != RunStatus.AWAITING_APPROVAL
            or previous.pending_approval is None
            or previous.run_context is None
        ):
            raise CloudConflict("run has no pending proposal")
        app = self._registered(run)
        app.validate(previous.run_context, run.definition.binding_digest)
        call = previous.pending_approval.call
        if (
            not app.application.policy.can_approve(
                requester=self._reviewer(previous.run_context, context.actor_id),
                owner=previous.run_context,
                call=call,
            )
            or context.actor_id == row["owner_id"]
        ):
            raise CloudDenied("independent approval required")
        request = governance_request(tool_envelope(app, call))
        return {
            "run_id": run.run_id,
            "application_id": run.definition.application_id,
            "fingerprint": approval_fingerprint(run, request),
            "proposal": call.model_dump(mode="json"),
        }

    async def approval_detail(
        self, principal: CurrentPrincipal, org_id: str, workspace_id: str, run_id: str
    ) -> dict[str, Any]:
        context = self._context(principal, org_id)
        async with self.store.transaction(context, "approve") as connection:
            row = await self._bound(connection, context, workspace_id, run_id, approval=True)
            return await self._proposal(connection, context, row)

    async def pending_approvals(
        self,
        principal: CurrentPrincipal,
        org_id: str,
        workspace_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        context = self._context(principal, org_id)
        after = self._page(limit, cursor)
        async with self.store.transaction(context, "approve") as connection:
            rows = await connection.fetch(
                "SELECT r.run_id FROM fh_cloud.runs r "
                "JOIN fh_cloud.run_bindings b USING(org_id,run_id) "
                "JOIN fh_cloud.deployment_grants g ON g.org_id=b.org_id "
                "AND g.deployment_id=b.deployment_id AND g.account_id=$2 "
                "WHERE r.org_id=$1 AND b.workspace_id=$3 AND r.run_id>$4 "
                "AND r.status='awaiting_approval' AND NOT r.cancel_requested "
                "AND r.owner_id<>$2 AND g.active AND g.can_approve "
                "AND NOT EXISTS(SELECT 1 FROM fh_cloud.approval_decisions d "
                "JOIN fh_cloud.approvals a ON a.org_id=d.org_id "
                "AND a.approval_id=d.approval_id WHERE d.org_id=r.org_id "
                "AND d.run_id=r.run_id AND NOT a.consumed) "
                "ORDER BY r.run_id LIMIT $5",
                org_id,
                context.actor_id,
                workspace_id,
                after,
                limit + 1,
            )
            items = []
            for row in rows[:limit]:
                bound = await self._bound(
                    connection,
                    context,
                    workspace_id,
                    row["run_id"],
                    approval=True,
                )
                items.append(await self._proposal(connection, context, bound))
            return {
                "items": items,
                "next_cursor": (rows[limit - 1]["run_id"] if len(rows) > limit else None),
            }

    async def decide(
        self,
        principal: CurrentPrincipal,
        org_id: str,
        workspace_id: str,
        run_id: str,
        decision: Literal["approve", "reject"],
        fingerprint: str,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"} or len(fingerprint) != 64:
            raise ValueError("invalid approval decision")
        context = self._context(principal, org_id)
        async with self.store.transaction(context, "approve") as connection:
            row = await self._bound(
                connection,
                context,
                workspace_id,
                run_id,
                approval=True,
                lock=True,
            )
            if row["owner_id"] == context.actor_id:
                raise CloudDenied("independent approval required")
            prior = await connection.fetchrow(
                "SELECT * FROM fh_cloud.approval_decisions "
                "WHERE org_id=$1 AND run_id=$2 AND action_digest=$3",
                org_id,
                run_id,
                fingerprint,
            )
            if prior is not None:
                if prior["decision"] != decision:
                    raise CloudConflict("proposal already decided")
                return {"run_id": run_id, "decision": decision, "fingerprint": fingerprint}
            proposal = await self._proposal(connection, context, row)
            if proposal["fingerprint"] != fingerprint:
                raise CloudConflict("reviewed proposal changed")
            await self.store.authorize_bound_run(connection, org_id, run_id)
            now = await connection.fetchval("SELECT clock_timestamp()")
            if self.store.record(row).definition.deadline_at <= now:
                raise CloudConflict("proposal expired")
            approval_id = None
            if decision == "approve":
                approval_id = "approval-" + uuid4().hex
                await connection.execute(
                    "INSERT INTO fh_cloud.approvals "
                    "(org_id,approval_id,run_id,action_digest,approver_id,expires_at) "
                    "VALUES($1,$2,$3,$4,$5,clock_timestamp()+interval '300 seconds')",
                    org_id,
                    approval_id,
                    run_id,
                    fingerprint,
                    context.actor_id,
                )
            else:
                await connection.execute(
                    "UPDATE fh_cloud.runs SET cancel_requested=true WHERE org_id=$1 AND run_id=$2",
                    org_id,
                    run_id,
                )
            await connection.execute(
                "INSERT INTO fh_cloud.approval_decisions "
                "(org_id,run_id,action_digest,decision,decider_id,approval_id) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                org_id,
                run_id,
                fingerprint,
                decision,
                context.actor_id,
                approval_id,
            )
            await self.store.emit(
                connection,
                org_id,
                run_id,
                "approval." + decision,
                {"action_digest": fingerprint, "decider_id": context.actor_id},
            )
        return {"run_id": run_id, "decision": decision, "fingerprint": fingerprint}

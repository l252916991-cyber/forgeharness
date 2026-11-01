"""Explicit administrator-only provisioning, separate from runtime startup.

The authenticated control plane owns organization/member provisioning. Never
give the runtime membership in the migration role or expose this to clients.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any


async def migrate(connection: Any) -> None:
    """Apply the initial schema once; existing/partial schemas fail closed."""
    source = files("forgeharness.cloud").joinpath("schema.sql").read_text()
    async with connection.transaction():
        await connection.execute(source)
        await upgrade_1_to_2(connection)
        await upgrade_2_to_3(connection, writers_stopped=True)


def schema_digest(source_root: Path | None = None) -> str:
    """Identity includes the ordered initial schema and every shipped migration."""
    root = source_root if source_root is not None else files("forgeharness.cloud")
    inputs = {"schema.sql": hashlib.sha256(root.joinpath("schema.sql").read_bytes()).hexdigest()}
    for name in ("002-execution-evidence.sql", "003-control-plane.sql"):
        inputs["migrations/" + name] = hashlib.sha256(
            root.joinpath("migrations", name).read_bytes()
        ).hexdigest()
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def upgrade_1_to_2(connection: Any) -> None:
    """Explicit, transactional upgrade. Existing workers must be stopped first."""
    async with connection.transaction():
        await connection.execute("LOCK TABLE fh_cloud.schema_version IN ACCESS EXCLUSIVE MODE")
        if await connection.fetchval("SELECT version FROM fh_cloud.schema_version") != 1:
            raise ValueError("execution-evidence migration requires exact schema version 1")
        source = files("forgeharness.cloud").joinpath("migrations", "002-execution-evidence.sql")
        await connection.execute(source.read_text())


async def control_plane_inventory(connection: Any) -> dict[str, int]:
    """Offline upgrade inventory; callers retain this before stopping writers."""
    return {
        "active_runs": int(
            await connection.fetchval(
                "SELECT count(*) FROM fh_cloud.runs WHERE status IN "
                "('queued','running','awaiting_approval','reconciling')"
            )
        ),
        "pending_approvals": int(
            await connection.fetchval("SELECT count(*) FROM fh_cloud.approvals WHERE NOT consumed")
        ),
        "unknown_calls": int(
            await connection.fetchval(
                "SELECT count(*) FROM fh_cloud.invocations WHERE state='unknown'"
            )
        ),
        "pending_outbox": int(
            await connection.fetchval(
                "SELECT count(*) FROM fh_cloud.outbox WHERE delivered_at IS NULL"
            )
        ),
    }


async def upgrade_2_to_3(connection: Any, *, writers_stopped: bool = False) -> None:
    """Explicit offline upgrade; never called by runtime or public API startup.

    The operator must inventory work and stop all API writers/workers first.
    Existing legacy runs remain legacy runs, including unknown reservations.
    """
    if writers_stopped is not True:
        raise ValueError("control-plane migration requires stopped API writers and workers")
    async with connection.transaction():
        await connection.execute("LOCK TABLE fh_cloud.schema_version IN ACCESS EXCLUSIVE MODE")
        if await connection.fetchval("SELECT version FROM fh_cloud.schema_version") != 2:
            raise ValueError("control-plane migration requires exact schema version 2")
        source = files("forgeharness.cloud").joinpath("migrations", "003-control-plane.sql")
        await connection.execute(source.read_text())


async def grant_runtime(connection: Any, role: str) -> None:
    """Grant only required runtime operations to a separately created role."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role):
        raise ValueError("runtime role needs a simple lowercase PostgreSQL identifier")
    async with connection.transaction():
        await connection.execute(f"GRANT USAGE ON SCHEMA fh_cloud TO {role}")
        await connection.execute(
            "GRANT SELECT ON fh_cloud.schema_version,fh_cloud.organizations,fh_cloud.members "
            f"TO {role}"
        )
        # PostgreSQL row locks require UPDATE on at least one column. This inert
        # column conveys no permission to change active/execute/approve grants.
        await connection.execute(
            f"GRANT UPDATE(lock_marker) ON fh_cloud.organizations,fh_cloud.members TO {role}"
        )
        await connection.execute(
            f"GRANT SELECT,INSERT,UPDATE ON fh_cloud.runs,fh_cloud.invocations TO {role}"
        )
        await connection.execute(
            f"GRANT SELECT,INSERT ON fh_cloud.outbox,fh_cloud.approvals,fh_cloud.receipts TO {role}"
        )
        await connection.execute(f"GRANT UPDATE(consumed) ON fh_cloud.approvals TO {role}")
        await connection.execute(f"GRANT UPDATE(delivered_at) ON fh_cloud.outbox TO {role}")
        await connection.execute(
            "GRANT SELECT,INSERT,UPDATE ON fh_cloud.execution_checkpoints,fh_cloud.execution_calls "
            f"TO {role}"
        )
        await connection.execute(
            "GRANT SELECT,INSERT ON fh_cloud.execution_evidence,fh_cloud.execution_logical_usage "
            f"TO {role}"
        )
        if await connection.fetchval("SELECT version FROM fh_cloud.schema_version") == 3:
            for table in (
                "org_members",
                "workspaces",
                "workspace_members",
                "deployments",
                "deployment_grants",
            ):
                await connection.execute(f"GRANT SELECT ON fh_cloud.{table} TO {role}")
                await connection.execute(f"GRANT UPDATE(lock_marker) ON fh_cloud.{table} TO {role}")
            await connection.execute(
                "GRANT SELECT(account_id,active,lock_marker),UPDATE(lock_marker) "
                f"ON fh_cloud.accounts TO {role}"
            )
            await connection.execute(
                "GRANT SELECT,INSERT ON fh_cloud.run_bindings,fh_cloud.approval_decisions "
                f"TO {role}"
            )


async def grant_identity(connection: Any, role: str) -> None:
    """Identity DML only; no execution facts, lease mutations, DDL or execute grant.

    Run and identity pools must use distinct database login roles. The identity
    role cannot grant ``can_execute`` even through the legacy member projection.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role):
        raise ValueError("identity role needs a simple lowercase PostgreSQL identifier")
    async with connection.transaction():
        await connection.execute(f"GRANT USAGE ON SCHEMA fh_cloud TO {role}")
        await connection.execute(f"GRANT SELECT ON fh_cloud.schema_version TO {role}")
        await connection.execute(f"GRANT SELECT,INSERT,UPDATE ON fh_cloud.accounts TO {role}")
        for table in (
            "org_members",
            "workspaces",
            "workspace_members",
            "deployments",
            "deployment_grants",
            "invitations",
            "sessions",
            "login_failures",
        ):
            await connection.execute(f"GRANT SELECT,INSERT,UPDATE ON fh_cloud.{table} TO {role}")
        await connection.execute(f"GRANT SELECT,INSERT ON fh_cloud.identity_audit TO {role}")
        await connection.execute(f"GRANT SELECT,INSERT ON fh_cloud.authentication_audit TO {role}")
        await connection.execute(
            f"GRANT SELECT,INSERT(org_id,active),UPDATE(active,lock_marker) "
            f"ON fh_cloud.organizations TO {role}"
        )
        await connection.execute(
            f"GRANT SELECT,INSERT(org_id,actor_id,active,can_approve),"
            f"UPDATE(active,can_approve,lock_marker) ON fh_cloud.members TO {role}"
        )


async def grant_web(connection: Any, role: str) -> None:
    """Public API DML; no physical sends, leases, checkpoint updates or settlement."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role):
        raise ValueError("web role needs a simple lowercase PostgreSQL identifier")
    async with connection.transaction():
        if await connection.fetchval("SELECT version FROM fh_cloud.schema_version") != 3:
            raise ValueError("web role requires exact schema version 3")
        await connection.execute(f"GRANT USAGE ON SCHEMA fh_cloud TO {role}")
        await connection.execute(
            "GRANT SELECT ON fh_cloud.schema_version,fh_cloud.organizations,fh_cloud.members "
            f"TO {role}"
        )
        await connection.execute(
            f"GRANT UPDATE(lock_marker) ON fh_cloud.organizations,fh_cloud.members TO {role}"
        )
        await connection.execute(f"GRANT SELECT,INSERT ON fh_cloud.runs TO {role}")
        await connection.execute(f"GRANT UPDATE(cancel_requested) ON fh_cloud.runs TO {role}")
        await connection.execute(
            "GRANT SELECT,INSERT ON fh_cloud.execution_checkpoints,fh_cloud.outbox,"
            "fh_cloud.approvals,fh_cloud.run_bindings,fh_cloud.approval_decisions "
            f"TO {role}"
        )
        for table in (
            "org_members",
            "workspaces",
            "workspace_members",
            "deployments",
            "deployment_grants",
        ):
            await connection.execute(f"GRANT SELECT ON fh_cloud.{table} TO {role}")
            await connection.execute(f"GRANT UPDATE(lock_marker) ON fh_cloud.{table} TO {role}")
        await connection.execute(
            "GRANT SELECT(account_id,active,lock_marker),UPDATE(lock_marker) "
            f"ON fh_cloud.accounts TO {role}"
        )

"""Explicit migration keeps governance facts and rolls back partial DDL."""

from importlib.resources import files

import asyncpg
import pytest

from forgeharness.cloud.execution_state import ExecutionStateStore
from forgeharness.cloud.models import CloudConflict, CloudDenied
from forgeharness.cloud.provision import grant_runtime, upgrade_1_to_2
from forgeharness.cloud.store import PostgresRunStore

from .test_run_store import definition


async def legacy_schema(pgcase):
    await pgcase.store.close()
    await pgcase.admin.execute("DROP SCHEMA fh_cloud CASCADE")
    await pgcase.admin.execute(files("forgeharness.cloud").joinpath("schema.sql").read_text())
    await pgcase.admin.execute(f"GRANT USAGE ON SCHEMA fh_cloud TO {pgcase.role}")
    await pgcase.admin.execute(f"GRANT SELECT ON fh_cloud.schema_version TO {pgcase.role}")


async def test_explicit_upgrade_preserves_unknown_cost_approval_and_identity(pgcase):
    await legacy_schema(pgcase)
    owner = await pgcase.seed()
    await pgcase.seed(actor_id="reviewer", can_approve=True)
    await pgcase.admin.execute(
        "INSERT INTO fh_cloud.runs(org_id,run_id,owner_id,request_key,definition,status,"
        "used_units,reserved_units,epoch) VALUES($1,'legacy-run',$2,'legacy',$3::jsonb,"
        "'reconciling',2,4,1)",
        owner.org_id,
        owner.actor_id,
        definition().model_dump_json(),
    )
    await pgcase.admin.execute(
        "INSERT INTO fh_cloud.approvals "
        "(org_id,approval_id,run_id,action_digest,approver_id,expires_at,consumed) "
        "VALUES($1,'legacy-approval','legacy-run',$2,'reviewer',clock_timestamp(),true)",
        owner.org_id,
        "a" * 64,
    )
    await pgcase.admin.execute(
        "INSERT INTO fh_cloud.invocations "
        "(org_id,run_id,invocation_id,tool_name,effect_class,arguments_digest,action_digest,"
        "approval_id,reserve_units,state,admission_epoch) VALUES "
        "($1,'legacy-run','legacy-call','write','idempotent_write',$2,$2,'legacy-approval',4,'unknown',1)",
        owner.org_id,
        "a" * 64,
    )
    before = {
        name: [dict(row) for row in await pgcase.admin.fetch(f"SELECT * FROM fh_cloud.{name}")]
        for name in ("runs", "approvals", "invocations")
    }
    with pytest.raises(CloudConflict, match="unsupported"):
        await PostgresRunStore.connect(pgcase.runtime_dsn)
    await upgrade_1_to_2(pgcase.admin)
    await grant_runtime(pgcase.admin, pgcase.role)
    assert await pgcase.admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 2
    for name, rows in before.items():
        after = [dict(row) for row in await pgcase.admin.fetch(f"SELECT * FROM fh_cloud.{name}")]
        assert len(after) == len(rows)
        # The migration adds columns, so an exact dict comparison could never hold.
        # What must hold is that every pre-existing column keeps its value verbatim;
        # the added columns are checked separately below against their audited default.
        kept = [
            {key: row[key] for key in expected} for row, expected in zip(after, rows, strict=True)
        ]
        assert kept == rows
    # A legacy invocation predates authorisation modes. It carried an approval id and no
    # governing grant column, so the only compatible reading is the independent-approval
    # default: widening it to policy-only authorisation would grant work nobody approved.
    legacy_row = await pgcase.admin.fetch(
        "SELECT authorization_mode, receipt_id FROM fh_cloud.invocations"
    )
    assert [dict(row) for row in legacy_row] == [
        {"authorization_mode": "approval", "receipt_id": None}
    ]
    restarted = await PostgresRunStore.connect(pgcase.runtime_dsn)
    try:
        record = await restarted.get(owner, "legacy-run")
        assert record.used_units == 2 and record.reserved_units == 4
        assert record.status == "reconciling" and record.epoch == 1
        with pytest.raises(CloudDenied):
            await ExecutionStateStore(restarted).load(owner, "legacy-run")
    finally:
        await restarted.close()


async def test_migration_failure_rolls_back_version_and_created_tables(pgcase):
    await legacy_schema(pgcase)
    await pgcase.admin.execute("CREATE TABLE fh_cloud.execution_evidence(sentinel INTEGER)")
    with pytest.raises(asyncpg.DuplicateTableError):
        await upgrade_1_to_2(pgcase.admin)
    assert await pgcase.admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 1
    assert (
        await pgcase.admin.fetchval("SELECT to_regclass('fh_cloud.execution_checkpoints')") is None
    )
    constraint = await pgcase.admin.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='fh_cloud.runs'::regclass AND conname='runs_status_check'"
    )
    assert "awaiting_approval" not in constraint


async def test_upgrade_requires_exact_source_schema_version(pgcase):
    with pytest.raises(ValueError, match="exact schema version 1"):
        await upgrade_1_to_2(pgcase.admin)
    assert await pgcase.admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 3

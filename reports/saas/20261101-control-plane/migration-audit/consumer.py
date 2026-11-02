"""Additional installed-wheel schema2 -> schema3 migration audit.

This independent consumer imports no repository source, tests, evals or fixtures.
It owns its socket-only PostgreSQL and uses synthetic in-process models only.
The final wheel is an explicit input whose complete installed package bytes are
verified before any database is created. Run with the installed venv's python -I.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import re
import sys
import traceback
import zipfile
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import embedded_postgres

import forgeharness
from forgeharness.cloud.calls import PostgresCallStore
from forgeharness.cloud.evidence import BufferedEvidence
from forgeharness.cloud.execution_contract import PhysicalResult
from forgeharness.cloud.hosted import HostedApplication
from forgeharness.cloud.models import CallRequest, Receipt, RunDefinition, TenantContext
from forgeharness.cloud.provision import (
    control_plane_inventory,
    grant_runtime,
    upgrade_1_to_2,
    upgrade_2_to_3,
)
from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.cloud.store import PostgresRunStore
from forgeharness.domain.models import (
    FinalAction,
    MessageRole,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.verification import VerificationResult
from forgeharness.sdk.spec import ApplicationSpec
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType

PROTOCOL_VERSION = 1
MODULES = (
    "forgeharness",
    "forgeharness.cloud.calls",
    "forgeharness.cloud.evidence",
    "forgeharness.cloud.execution",
    "forgeharness.cloud.execution_calls",
    "forgeharness.cloud.execution_state",
    "forgeharness.cloud.hosted",
    "forgeharness.cloud.provision",
    "forgeharness.cloud.runtime",
    "forgeharness.cloud.store",
)
FINAL = "synthetic migration history verified"


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def persist(output: Path, name: str, value: Any) -> str:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    (output / name).write_bytes(data)
    return sha(data)


def validate_installation(wheel: Path) -> dict[str, Any]:
    package = Path(forgeharness.__file__).resolve().parent
    prefix = Path(sys.prefix).resolve()
    assert "site-packages" in package.parts and package.is_relative_to(prefix)
    assert sys.flags.isolated, "consumer requires python -I"
    checked = 0
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            if item.filename.startswith("forgeharness/") and not item.is_dir():
                relative = item.filename.removeprefix("forgeharness/")
                installed = package / relative
                assert installed.is_file() and installed.read_bytes() == archive.read(item)
                checked += 1
    assert checked > 0
    module_paths = {}
    for name in MODULES:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(package)
        module_paths[name] = str(path)
    return {
        "loaded_from_installation": True,
        "all_installed_package_files_match_wheel": True,
        "installed_wheel_files_checked": checked,
        "installed_version": importlib.metadata.version("forgeharness"),
        "module_paths": module_paths,
        "python_isolated": True,
    }


class Policy:
    def evaluate(self, request: Any) -> PolicyDecision:
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="synthetic audit policy")

    def can_approve(self, **values: Any) -> bool:
        return False


class Model:
    def __init__(self, unknown_tool_first: bool) -> None:
        self.unknown_tool_first = unknown_tool_first

    async def decide(self, request: Any) -> ModelResult:
        observed = any(message.role == MessageRole.TOOL for message in request.messages)
        action = (
            ToolAction(
                call=ToolCall(id="legacy-logical-denial", name="migration.unknown", arguments={})
            )
            if self.unknown_tool_first and not observed
            else FinalAction(content=FINAL)
        )
        return ModelResult(
            action=action,
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            model_name="migration-synthetic-v1",
        )


class Verifier:
    async def verify(self, request: Any) -> VerificationResult:
        return VerificationResult(
            passed=request.final.content == FINAL,
            reason="synthetic fixed result checked for migration audit",
        )


def registration(name: str, *, unresolved: bool = False) -> HostedApplication:
    async def verify_model(physical: Any, outcome: Any) -> PhysicalResult | None:
        if unresolved:
            return None
        assert outcome.result is not None
        assert outcome.result.model_name == "migration-synthetic-v1"
        assert outcome.result.usage == ModelUsage(input_tokens=3, output_tokens=2)
        return PhysicalResult(
            receipt_id="receipt-" + sha(physical.idempotency_key.encode())[:32],
            actual_units=7,
            kind="model",
            model_result=outcome.result,
        )

    async def verify_tool(physical: Any, outcome: Any) -> None:
        raise AssertionError("no physical tool is registered")

    app = ApplicationSpec(
        id="migration-" + name,
        version="legacy-v1",
        policy_version="legacy-policy-v1",
        model_factory=lambda: Model(not unresolved),
        tools=(),
        policy=Policy(),
        verifier=Verifier(),
        budget=RunBudget(
            max_steps=5,
            max_tool_calls=2,
            max_input_tokens=10000,
            max_output_tokens=1000,
            reserve_output_tokens=10,
            max_attempts_per_call=1,
            max_wall_seconds=600,
        ),
        instructions=("Use only the fixed synthetic migration fixture.",),
    )
    return HostedApplication(
        application=app,
        deployment_id="migration-" + name,
        release_digest=sha(("synthetic-release:" + name).encode()),
        model_binding_digest=sha(("synthetic-model:" + name).encode()),
        max_run_units=100,
        model_reservation_units=9,
        tool_reservation_units={},
        model_verifier=verify_model,
        tool_verifier=verify_tool,
        resources=("legacy-material",),
        roles=("legacy-member",),
    )


def scoped_dsn(dsn: str, *, database: str, role: str) -> str:
    parsed = urlsplit(dsn)
    query = dict(parse_qsl(parsed.query)) | {"user": role, "database": database}
    return urlunsplit(
        (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], "/" + database, urlencode(query), "")
    )


async def snapshot(connection: Any, tables: list[str]) -> dict[str, list[str]]:
    # row_to_json preserves every column value; JSONB's authoritative PG text
    # representation is compared byte-for-byte, not selected-field projections.
    return {
        name: sorted(
            [
                row["encoded"]
                for row in await connection.fetch(
                    f"SELECT row_to_json(t)::text AS encoded FROM fh_cloud.{name} t"
                )
            ]
        )
        for name in tables
    }


async def historical_reads(
    runtime: HostedRuntime, owner: TenantContext, run_ids: list[str]
) -> dict[str, Any]:
    result = {}
    for run_id in run_ids:
        view = await runtime.inspect(owner, run_id)
        evidence = await runtime.trace(owner, run_id)
        serialized = evidence.export_json()
        envelopes = json.loads(serialized)
        head = envelopes[-1]["hash"] if envelopes else ""
        restored = BufferedEvidence.from_json(run_id, serialized, expected_head=head)
        assert restored.export_json() == serialized
        assert view.checkpoint.run_context is not None
        assert view.checkpoint.run_context.actor.id == "legacy-owner"
        result[run_id] = {
            "view_json": view.model_dump_json(),
            "trace_json": serialized,
            "trace_sha256": sha(serialized.encode()),
            "trace_head": head,
            "trace_events": len(envelopes),
        }
    return result


async def exercise(dsn: str, output: Path) -> dict[str, Any]:
    owner_connection = await asyncpg.connect(dsn)
    database = "fh_migration_" + uuid4().hex
    role = "fh_migration_runtime_" + uuid4().hex
    admin = store = None
    database_created = role_created = False
    result: dict[str, Any] = {}
    try:
        await owner_connection.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
        role_created = True
        await owner_connection.execute(f"CREATE DATABASE {database}")
        database_created = True
        admin = await asyncpg.connect(
            scoped_dsn(
                dsn, database=database, role=await owner_connection.fetchval("SELECT current_user")
            )
        )
        cloud = files("forgeharness.cloud")
        await admin.execute(cloud.joinpath("schema.sql").read_text())
        await upgrade_1_to_2(admin)
        await grant_runtime(admin, role)
        assert await admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 2
        await admin.execute(
            "INSERT INTO fh_cloud.organizations(org_id) VALUES('legacy-org'),('other-org')"
        )
        await admin.execute(
            "INSERT INTO fh_cloud.members(org_id,actor_id,can_execute,can_approve) VALUES "
            "('legacy-org','legacy-owner',false,false),"
            "('legacy-org','legacy-worker',true,false),"
            "('legacy-org','legacy-reviewer',false,true),"
            "('other-org','legacy-owner',false,false)"
        )
        store = await PostgresRunStore.connect(scoped_dsn(dsn, database=database, role=role))
        complete, unknown = registration("complete"), registration("unknown", unresolved=True)
        runtime = HostedRuntime(store, {item.deployment_id: item for item in (complete, unknown)})
        owner = TenantContext(org_id="legacy-org", actor_id="legacy-owner")
        worker = TenantContext(org_id="legacy-org", actor_id="legacy-worker")
        reviewer = TenantContext(org_id="legacy-org", actor_id="legacy-reviewer")
        run_ids = []
        for app in (complete, unknown):
            accepted = await runtime.accept(
                owner, app.deployment_id, app.deployment_id, "Synthetic legacy migration task"
            )
            run_ids.append(accepted.run.run_id)
            await runtime.advance(worker, accepted.run.run_id, worker_id="migration-worker")
        complete_view = await runtime.inspect(owner, run_ids[0])
        unknown_view = await runtime.inspect(owner, run_ids[1])
        assert complete_view.run.status == "succeeded"
        assert complete_view.checkpoint.status == RunStatus.SUCCEEDED
        assert complete_view.run.used_units == 14 and complete_view.run.reserved_units == 0
        assert complete_view.checkpoint.usage.tool_calls == 1
        assert unknown_view.run.status == "reconciling" and unknown_view.run.reserved_units == 9
        assert unknown_view.run.used_units == 0
        governance = await store.submit(
            owner,
            "legacy-approved-write",
            RunDefinition(
                application_id="legacy-governance",
                application_version="1",
                policy_version="1",
                binding_digest="b" * 64,
                input_digest="c" * 64,
                max_units=50,
                max_calls=5,
                deadline_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
        )
        calls = PostgresCallStore(store)
        request = CallRequest(
            invocation_id="legacy-approved-call",
            tool_name="legacy.write",
            effect_class="idempotent_write",
            arguments_digest="d" * 64,
            reserve_units=5,
        )
        approval_id = await calls.grant(reviewer, governance.run_id, request)
        ticket = await store.claim(worker, governance.run_id, "migration-governance-worker")
        await calls.admit(worker, ticket, request, approval_id)
        await calls.mark_sent(worker, ticket, request.invocation_id)
        await calls.record_receipt(
            worker,
            governance.run_id,
            request.invocation_id,
            Receipt(receipt_id="legacy-approved-receipt", actual_units=3, result_digest="e" * 64),
        )
        await calls.settle(worker, ticket, request.invocation_id)
        await store.finish(worker, ticket)
        tables = [
            row["tablename"]
            for row in await admin.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='fh_cloud' "
                "AND tablename<>'schema_version' ORDER BY tablename"
            )
        ]
        before = await snapshot(admin, tables)
        assert all(before[name] for name in tables), "every schema2 fact table must be nonempty"
        reads_before = await historical_reads(runtime, owner, run_ids)
        checkpoints = [json.loads(row) for row in before["execution_checkpoints"]]
        assert any(any(v > 0 for v in row["used_usage"].values()) for row in checkpoints)
        assert any(any(v > 0 for v in row["reserved_usage"].values()) for row in checkpoints)
        result["before_snapshot_sha256"] = persist(output, "before.json", before)
        result["historical_reads_before_sha256"] = persist(
            output, "reads-before.json", reads_before
        )
        result["schema2_tables"] = tables
        result["rows_per_table"] = {name: len(rows) for name, rows in before.items()}
        result["inventory_before"] = await control_plane_inventory(admin)
        result["unknown_reserved_units_before"] = unknown_view.run.reserved_units
        result["checkpoint_budgets_and_usage_nonempty"] = True
        result["actual_hosted_runtime_generated_state"] = True
        result["logical_denial_and_physical_result_facts_present"] = True
        # Inject a failure after accounts and org_members would have been created.
        constraint_before = await admin.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='fh_cloud.schema_version'::regclass "
            "AND conname='schema_version_version_check'"
        )
        await admin.execute("CREATE TABLE fh_cloud.workspaces(sentinel INTEGER)")
        try:
            await upgrade_2_to_3(admin, writers_stopped=True)
        except asyncpg.DuplicateTableError:
            result["injected_failure_type"] = "DuplicateTableError"
        else:
            raise AssertionError("injected migration failure did not fail")
        assert await admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 2
        assert await admin.fetchval("SELECT to_regclass('fh_cloud.accounts')") is None
        assert await admin.fetchval("SELECT to_regclass('fh_cloud.org_members')") is None
        assert (
            await admin.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='fh_cloud.schema_version'::regclass "
                "AND conname='schema_version_version_check'"
            )
            == constraint_before
        )
        assert await snapshot(admin, tables) == before
        assert await historical_reads(runtime, owner, run_ids) == reads_before
        result["failed_migration_ddl_and_all_legacy_facts_rolled_back"] = True
        await admin.execute("DROP TABLE fh_cloud.workspaces")
        await upgrade_2_to_3(admin, writers_stopped=True)
        assert await admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 3
        after = await snapshot(admin, tables)
        result["after_snapshot_sha256"] = persist(output, "after.json", after)
        assert after == before
        assert result["after_snapshot_sha256"] == result["before_snapshot_sha256"]
        result["all_schema2_rows_exactly_equal"] = True
        result["schema_version_changed_only_as_expected"] = True
        assert await admin.fetchval("SELECT count(*) FROM fh_cloud.accounts") == 0
        assert await admin.fetchval("SELECT count(*) FROM fh_cloud.run_bindings") == 0
        result["no_automatic_account_or_run_adoption"] = True
        await grant_runtime(admin, role)
        result["schema3_runtime_grants_explicitly_refreshed"] = True
        reads_after = await historical_reads(runtime, owner, run_ids)
        assert reads_after == reads_before
        result["historical_reads_after_sha256"] = persist(output, "reads-after.json", reads_after)
        assert result["historical_reads_before_sha256"] == result["historical_reads_after_sha256"]
        assert await snapshot(admin, tables) == before
        result["post_upgrade_trusted_host_inspect_trace_exactly_equal"] = True
        result["old_owner_unchanged"] = True
        result["unknown_reservation_preserved_without_redispatch"] = True
        result["inventory_after"] = await control_plane_inventory(admin)
        assert result["inventory_after"] == result["inventory_before"]
        result["evidence_chains"] = {
            key: {field: value[field] for field in ("trace_head", "trace_sha256", "trace_events")}
            for key, value in reads_after.items()
        }
        result["passed"] = True
        return result
    finally:
        if store is not None:
            await store.close()
        if admin is not None:
            await admin.close()
        if database_created:
            await owner_connection.execute(f"DROP DATABASE {database} WITH (FORCE)")
        if role_created:
            await owner_connection.execute(f"DROP ROLE {role}")
        await owner_connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).resolve()
    protocol = script.with_name("protocol.json")
    result: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "script_sha256": sha(script.read_bytes()),
        "protocol_sha256": sha(protocol.read_bytes()),
        "wheel_path": str(wheel),
        "wheel_sha256": sha(wheel.read_bytes()),
        "audit_type": "additional independent installed-artifact migration audit",
        "model": "deterministic in-process synthetic model; no model service calls",
        "network": "private PostgreSQL Unix socket only",
        "server_stopped": False,
        "passed": False,
    }
    try:
        result.update(validate_installation(wheel))
        with TemporaryDirectory(prefix="owned-pg-", dir=output) as temporary:
            server = embedded_postgres.get_server(Path(temporary) / "pgdata", cleanup_mode="stop")
            try:
                version = server.psql("SHOW server_version").strip()
                assert "18.6" in version
                assert re.search(
                    r"^\s*t\s*$",
                    server.psql("SELECT current_setting('listen_addresses')=''"),
                    re.MULTILINE,
                )
                result["postgres_version"] = "18.6"
                result.update(asyncio.run(exercise(server.get_uri(), output)))
            finally:
                server.cleanup()
                result["server_stopped"] = True
    except BaseException as exc:
        # No arbitrary SQL, connection strings, secrets or exception text in summary.
        result["passed"] = False
        result["error_type"] = type(exc).__name__
        own_lines = [
            frame.lineno
            for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).resolve() == script
        ]
        result["consumer_error_line"] = own_lines[-1] if own_lines else None
    result["passed"] = bool(result["passed"] and result["server_stopped"])
    persist(output, "summary.json", result)
    print(encode({"passed": result["passed"], "summary": str(output / "summary.json")}))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()

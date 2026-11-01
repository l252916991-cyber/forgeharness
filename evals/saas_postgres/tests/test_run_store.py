"""Transactions, RLS and owner fencing against real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from forgeharness.cloud.models import (
    CloudConflict,
    CloudDenied,
    RunDefinition,
    StaleLease,
)
from forgeharness.cloud.store import PostgresRunStore


def definition(**changes) -> RunDefinition:
    return RunDefinition(
        application_id="synthetic-contract",
        application_version="1",
        policy_version="1",
        binding_digest="a" * 64,
        input_digest="b" * 64,
        max_units=10,
        deadline_at=datetime.now(UTC) + timedelta(minutes=10),
    ).model_copy(update=changes)


async def two_workers(pgcase, payloads: list[dict]) -> list[dict]:
    worker = Path(__file__).with_name("worker.py")
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=pgcase.worker_environment(),
        )
        for _ in payloads
    ]
    try:
        for process in processes:
            assert await asyncio.wait_for(process.stdout.readline(), 30) == b"READY\n"
        outputs = await asyncio.wait_for(
            asyncio.gather(
                *(
                    process.communicate(json.dumps(payload).encode() + b"\n")
                    for process, payload in zip(processes, payloads, strict=True)
                )
            ),
            30,
        )
        results = []
        for process, (output, error) in zip(processes, outputs, strict=True):
            assert process.returncode == 0, error.decode()
            results.append(json.loads(output))
        assert len({result["pid"] for result in results}) == len(results)
        return results
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_same_request_in_distinct_processes_creates_one_run_and_outbox(pgcase):
    ctx = await pgcase.seed()
    task = definition()
    payload = {
        "operation": "submit",
        "context": ctx.model_dump(mode="json"),
        "request_key": "same-intent",
        "definition": task.model_dump(mode="json"),
    }
    results = await two_workers(pgcase, [payload, payload])
    assert results[0]["run_id"] == results[1]["run_id"]
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.runs") == 1
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox") == 1
    with pytest.raises(CloudConflict):
        await pgcase.store.submit(
            ctx, "same-intent", task.model_copy(update={"input_digest": "c" * 64})
        )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.outbox") == 1


async def test_request_keys_are_scoped_by_current_owner_and_organization(pgcase):
    first = await pgcase.seed()
    second = await pgcase.seed(actor_id="actor-b", can_execute=False)
    foreign = await pgcase.seed(org_id="org-b", actor_id="actor-c")
    task = definition()
    runs = [await pgcase.store.submit(ctx, "public-key", task) for ctx in (first, second, foreign)]
    assert len({run.run_id for run in runs}) == 3
    for ctx in (second, foreign):
        with pytest.raises(CloudDenied):
            await pgcase.store.get(ctx, runs[0].run_id)
    with pytest.raises(CloudDenied):
        await pgcase.store.events(foreign, runs[0].run_id)


async def test_two_processes_cannot_both_own_one_run(pgcase):
    ctx = await pgcase.seed()
    run = await pgcase.store.submit(ctx, "claim-intent", definition())
    payload = {"operation": "claim", "context": ctx.model_dump(mode="json"), "run_id": run.run_id}
    results = await two_workers(
        pgcase,
        [payload | {"worker_id": "worker-a"}, payload | {"worker_id": "worker-b"}],
    )
    assert sorted(result["status"] for result in results) == ["claimed", "conflict"]
    assert (await pgcase.store.get(ctx, run.run_id)).epoch == 1


async def test_expired_owner_cannot_renew_or_commit_after_takeover(pgcase):
    ctx = await pgcase.seed()
    run = await pgcase.store.submit(ctx, "takeover", definition())
    old = await pgcase.store.claim(ctx, run.run_id, worker_id="old")
    await pgcase.admin.execute(
        "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()-interval '1 second' "
        "WHERE org_id=$1 AND run_id=$2",
        ctx.org_id,
        run.run_id,
    )
    with pytest.raises(StaleLease):
        await pgcase.store.renew(ctx, old)
    new = await pgcase.store.claim(ctx, run.run_id, worker_id="new")
    assert new.epoch == old.epoch + 1
    before = await pgcase.store.events(ctx, run.run_id)
    with pytest.raises(StaleLease):
        await pgcase.store.finish(ctx, old, status="failed")
    assert await pgcase.store.events(ctx, run.run_id) == before
    await pgcase.store.finish(ctx, new, status="failed")
    assert (await pgcase.store.get(ctx, run.run_id)).status == "failed"


async def test_runtime_rejects_superuser_connections(pgcase):
    with pytest.raises((CloudDenied, ValueError)):
        await PostgresRunStore.connect(pgcase.admin_dsn)


async def test_runtime_rejects_noinherit_membership_in_migration_role(pgcase):
    owner_role = await pgcase.admin.fetchval("SELECT current_user")
    await pgcase.admin.execute(f'GRANT "{owner_role}" TO {pgcase.role}')
    try:
        with pytest.raises(CloudDenied):
            await PostgresRunStore.connect(pgcase.runtime_dsn)
    finally:
        await pgcase.admin.execute(f'REVOKE "{owner_role}" FROM {pgcase.role}')


async def test_rls_has_no_default_tenant_and_pool_context_does_not_leak(pgcase):
    first = await pgcase.seed()
    second = await pgcase.seed(org_id="org-b", actor_id="actor-b")
    a = await pgcase.store.submit(first, "a", definition())
    await pgcase.store.submit(second, "b", definition())
    connection = await pgcase.app_connection()
    try:
        assert await connection.fetchval("SELECT COUNT(*) FROM fh_cloud.runs") == 0
        for ctx in (first, second, first):
            async with connection.transaction():
                await connection.execute("SELECT set_config('fh.org_id',$1,true)", ctx.org_id)
                await connection.execute("SELECT set_config('fh.actor_id',$1,true)", ctx.actor_id)
                rows = await connection.fetch("SELECT org_id FROM fh_cloud.runs")
                assert rows and all(row["org_id"] == ctx.org_id for row in rows)
            assert await connection.fetchval("SELECT COUNT(*) FROM fh_cloud.runs") == 0
        async with connection.transaction():
            await connection.execute("SELECT set_config('fh.org_id',$1,true)", second.org_id)
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM fh_cloud.runs WHERE run_id=$1", a.run_id
                )
                == 0
            )
    finally:
        await connection.close()


async def test_runtime_role_cannot_change_memberships_or_disable_rls(pgcase):
    await pgcase.seed()
    connection = await pgcase.app_connection()
    try:
        role = await connection.fetchrow(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
        assert not role["rolsuper"] and not role["rolbypassrls"]
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("UPDATE fh_cloud.members SET can_approve=true")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("ALTER TABLE fh_cloud.runs DISABLE ROW LEVEL SECURITY")
    finally:
        await connection.close()


async def test_revoked_membership_and_suspended_org_block_new_execution(pgcase):
    ctx = await pgcase.seed()
    run = await pgcase.store.submit(ctx, "before-revoke", definition())
    await pgcase.admin.execute(
        "UPDATE fh_cloud.members SET active=false WHERE org_id=$1 AND actor_id=$2",
        ctx.org_id,
        ctx.actor_id,
    )
    with pytest.raises(CloudDenied):
        await pgcase.store.claim(ctx, run.run_id, worker_id="worker")
    with pytest.raises(CloudDenied):
        await pgcase.store.submit(ctx, "after-revoke", definition())
    await pgcase.seed()
    await pgcase.admin.execute(
        "UPDATE fh_cloud.organizations SET active=false WHERE org_id=$1", ctx.org_id
    )
    with pytest.raises(CloudDenied):
        await pgcase.store.submit(ctx, "after-suspend", definition())


async def test_missing_or_foreign_objects_share_a_denial(pgcase):
    first = await pgcase.seed()
    foreign = await pgcase.seed(org_id="org-b", actor_id="actor-b")
    run = await pgcase.store.submit(first, "private", definition())
    errors = []
    for run_id in (run.run_id, "does-not-exist"):
        with pytest.raises(CloudDenied) as raised:
            await pgcase.store.get(foreign, run_id)
        errors.append(str(raised.value))
    assert errors[0] == errors[1]


async def test_outbox_events_remain_durable_until_acknowledged(pgcase):
    ctx = await pgcase.seed()
    task = definition()
    run = await pgcase.store.submit(ctx, "queue-lost", task)
    initial = await pgcase.store.events(ctx, run.run_id)
    assert len(initial) == 1
    # No queue is consulted: a fresh store connection reads the durable intent.
    restarted = await PostgresRunStore.connect(pgcase.runtime_dsn)
    try:
        await restarted.submit(ctx, "queue-lost", task)
        assert await restarted.events(ctx, run.run_id) == initial
    finally:
        await restarted.close()
    assert (
        await pgcase.admin.fetchval(
            "SELECT COUNT(*) FROM fh_cloud.outbox WHERE delivered_at IS NULL"
        )
        == 1
    )


async def test_privileged_session_cannot_hide_behind_startup_runtime_role(pgcase):
    parsed = urlsplit(pgcase.admin_dsn)
    query = dict(parse_qsl(parsed.query)) | {"role": pgcase.role, "database": pgcase.database}
    disguised = urlunsplit(
        (parsed.scheme, parsed.netloc, "/" + pgcase.database, urlencode(query), "")
    )
    with pytest.raises(CloudDenied, match="runtime role"):
        await PostgresRunStore.connect(disguised)

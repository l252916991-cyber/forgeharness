"""Disposable database fixtures for the cloud state transaction contract.

The test runner owns the entire Unix-socket server. These fixtures refuse to
connect without its disposable-server acknowledgement and create/drop only their
own randomly named databases and roles, never an existing application database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from forgeharness.cloud.models import TenantContext
from forgeharness.cloud.provision import grant_runtime, migrate
from forgeharness.cloud.store import PostgresRunStore


@dataclass
class PgCase:
    admin: Any
    store: PostgresRunStore
    admin_dsn: str = field(repr=False)
    runtime_dsn: str = field(repr=False)
    database: str
    role: str

    async def seed(
        self,
        org_id: str = "org-a",
        actor_id: str = "actor-a",
        *,
        can_execute: bool = True,
        can_approve: bool = False,
    ) -> TenantContext:
        await self.admin.execute(
            "INSERT INTO fh_cloud.organizations(org_id, active) VALUES($1,true) "
            "ON CONFLICT(org_id) DO NOTHING",
            org_id,
        )
        await self.admin.execute(
            "INSERT INTO fh_cloud.members(org_id, actor_id, active, can_execute, can_approve) "
            "VALUES($1,$2,true,$3,$4) "
            "ON CONFLICT(org_id,actor_id) DO UPDATE SET "
            "active=true,can_execute=EXCLUDED.can_execute,can_approve=EXCLUDED.can_approve",
            org_id,
            actor_id,
            can_execute,
            can_approve,
        )
        return TenantContext(org_id=org_id, actor_id=actor_id)

    async def app_connection(self) -> Any:
        return await asyncpg.connect(self.admin_dsn, database=self.database, user=self.role)

    def worker_environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            FH_TEST_PG_WORKER_DSN=self.admin_dsn,
            FH_TEST_PG_WORKER_DATABASE=self.database,
            FH_TEST_PG_WORKER_ROLE=self.role,
        )
        return env


@pytest.fixture
async def pgcase() -> AsyncIterator[PgCase]:
    if os.environ.get("FH_TEST_PG_DISPOSABLE") != "1":
        raise RuntimeError("real-PG tests require the disposable-server runner")
    dsn = os.environ.get("FH_TEST_PG_ADMIN_DSN")
    if not dsn:
        raise RuntimeError("disposable PostgreSQL administrator connection is required")
    database = "fh_test_" + uuid4().hex
    role = "fh_test_role_" + uuid4().hex
    owner = await asyncpg.connect(dsn)
    admin = None
    store = None
    database_created = role_created = False
    try:
        await owner.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
        role_created = True
        await owner.execute(f"CREATE DATABASE {database}")
        database_created = True
        admin = await asyncpg.connect(dsn, database=database)
        await migrate(admin)
        await grant_runtime(admin, role)
        # The URI is synthesized only for the local tool; it is never logged.
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parsed = urlsplit(dsn)
        host = parsed.netloc.rsplit("@", 1)[-1]
        query = dict(parse_qsl(parsed.query))
        query["user"] = role
        query["database"] = database
        runtime_dsn = urlunsplit((parsed.scheme, host, "/" + database, urlencode(query), ""))
        store = await PostgresRunStore.connect(runtime_dsn)
        yield PgCase(
            admin=admin,
            store=store,
            admin_dsn=dsn,
            runtime_dsn=runtime_dsn,
            database=database,
            role=role,
        )
    finally:
        if store is not None:
            await store.close()
        if admin is not None:
            await admin.close()
        if database_created:
            await owner.execute(f"DROP DATABASE {database} WITH (FORCE)")
        if role_created:
            await owner.execute(f"DROP ROLE {role}")
        await owner.close()

"""Real PostgreSQL credential, revocation, migration and role-boundary contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib.resources import files
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from forgeharness.cloud.identity import PostgresIdentityStore, authorize_deployment
from forgeharness.cloud.models import CloudConflict, CloudDenied, TenantContext
from forgeharness.cloud.provision import (
    control_plane_inventory,
    grant_identity,
    grant_web,
    upgrade_1_to_2,
    upgrade_2_to_3,
)
from forgeharness.cloud.store import PostgresRunStore

from .test_run_store import definition

PASSWORD = "synthetic-password-2468"


@dataclass
class IdentityCase:
    store: PostgresIdentityStore
    web: PostgresRunStore
    role: str
    web_role: str


@pytest.fixture
async def identitycase(pgcase):
    role = "fh_identity_" + uuid4().hex
    web_role = "fh_web_" + uuid4().hex
    for selected in (role, web_role):
        await pgcase.admin.execute(
            f"CREATE ROLE {selected} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
    await grant_identity(pgcase.admin, role)
    await grant_web(pgcase.admin, web_role)
    parsed = urlsplit(pgcase.runtime_dsn)
    query = dict(parse_qsl(parsed.query))
    query["user"] = role
    dsn = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    identity = await PostgresIdentityStore.connect(dsn)
    query["user"] = web_role
    web_dsn = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    web = await PostgresRunStore.connect(web_dsn)
    try:
        yield IdentityCase(identity, web, role, web_role)
    finally:
        await identity.close()
        await web.close()
        for selected in (role, web_role):
            await pgcase.admin.execute(f"DROP OWNED BY {selected}")
            await pgcase.admin.execute(f"DROP ROLE {selected}")


async def member(identity, owner, email="member@example.test", role="member"):
    token = await identity.invite(owner.principal, owner.principal.org_id, email, role)
    return await identity.accept_invitation(token, PASSWORD)


async def deployed(identity, owner, user, *, deployment="dep-a", workspace="ws-a"):
    org = owner.principal.org_id
    await identity.put_workspace(owner.principal, org, workspace, "Synthetic workspace")
    await identity.set_workspace_member(owner.principal, org, workspace, user.principal.account_id)
    await identity.put_deployment(
        owner.principal, org, deployment, workspace, "synthetic", "a" * 64
    )
    await identity.set_deployment_grant(owner.principal, org, deployment, user.principal.account_id)


async def test_bootstrap_invite_login_and_secret_digests(pgcase, identitycase):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "OWNER@example.test", PASSWORD)
    invited = await identity.invite(owner.principal, "org-a", "person@example.test")
    user = await identity.accept_invitation(invited, PASSWORD)
    assert user.principal.actor_id.startswith("acct_")
    assert user.principal.actor_id != owner.principal.actor_id
    assert (await identity.login("PERSON@example.test", PASSWORD)).principal.account_id == (
        user.principal.account_id
    )
    assert (await identity.check_csrf(user.token, user.csrf_token)) == user.principal
    assert await identity.session_csrf(user.token) == user.csrf_token
    for table in (
        "accounts",
        "sessions",
        "invitations",
        "identity_audit",
        "login_failures",
        "authentication_audit",
    ):
        serialized = str(
            [dict(row) for row in await pgcase.admin.fetch(f"SELECT * FROM fh_cloud.{table}")]
        )
        assert all(
            secret not in serialized
            for secret in (
                PASSWORD,
                owner.token,
                owner.csrf_token,
                invited,
                user.token,
                user.csrf_token,
            )
        )
    assert user.token not in repr(user)
    assert not await pgcase.admin.fetchval(
        "SELECT can_execute FROM fh_cloud.members WHERE org_id='org-a' AND actor_id=$1",
        user.principal.account_id,
    )
    with pytest.raises(CloudConflict):
        await identity.bootstrap("org-a", "other@example.test", PASSWORD)


async def test_invitation_once_only_concurrent_and_expired(pgcase, identitycase):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    token = await identity.invite(owner.principal, "org-a", "person@example.test")
    results = await asyncio.gather(
        identity.accept_invitation(token, PASSWORD),
        identity.accept_invitation(token, PASSWORD),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, CloudDenied) for item in results) == 1
    expired = await identity.invite(owner.principal, "org-a", "expired@example.test")
    await pgcase.admin.execute(
        "UPDATE fh_cloud.invitations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE email_normalized='expired@example.test'"
    )
    with pytest.raises(CloudDenied):
        await identity.accept_invitation(expired, PASSWORD)


async def test_invitation_management_paginates_safe_metadata_and_revokes_once(pgcase, identitycase):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    admin = await member(identity, owner, "admin@example.test", "admin")
    tokens = [
        await identity.invite(owner.principal, "org-a", f"person-{i}@example.test")
        for i in range(4)
    ]
    seen = []
    cursor = None
    while True:
        page = await identity.list_invitations(owner.principal, "org-a", 2, cursor)
        assert len(page["items"]) <= 2
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len({row["invitation_id"] for row in seen}) == 5
    assert all("token_digest" not in row for row in seen)
    pending = next(row for row in seen if row["email"] == "person-0@example.test")
    await identity.revoke_invitation(admin.principal, "org-a", pending["invitation_id"])
    first = await pgcase.admin.fetchval(
        "SELECT revoked_at FROM fh_cloud.invitations WHERE invitation_id=$1",
        pending["invitation_id"],
    )
    await identity.revoke_invitation(admin.principal, "org-a", pending["invitation_id"])
    assert (
        await pgcase.admin.fetchval(
            "SELECT revoked_at FROM fh_cloud.invitations WHERE invitation_id=$1",
            pending["invitation_id"],
        )
        == first
    )
    with pytest.raises(CloudDenied):
        await identity.accept_invitation(tokens[0], PASSWORD)
    with pytest.raises(ValueError):
        await identity.list_invitations(owner.principal, "org-a", 101)
    with pytest.raises(ValueError):
        await identity.list_invitations(owner.principal, "org-a", 2, "forged")


async def test_existing_account_cross_org_needs_current_session_and_keeps_password(
    pgcase,
    identitycase,
):
    identity = identitycase.store
    owner_a = await identity.bootstrap("org-a", "owner-a@example.test", PASSWORD)
    owner_b = await identity.bootstrap("org-b", "owner-b@example.test", PASSWORD)
    user = await member(identity, owner_a)
    old_hash = await pgcase.admin.fetchval(
        "SELECT password_hash FROM fh_cloud.accounts WHERE account_id=$1", user.principal.account_id
    )
    token = await identity.invite(owner_b.principal, "org-b", user.principal.email)
    for kwargs in (
        {"password": "attacker-password-123"},
        {},
        {"existing_session": owner_b.token},
        {"existing_session": user.token, "password": PASSWORD},
    ):
        with pytest.raises(CloudDenied):
            await identity.accept_invitation(token, **kwargs)
    other = await identity.accept_invitation(token, existing_session=user.token)
    assert other.principal.account_id == user.principal.account_id
    assert (
        await pgcase.admin.fetchval(
            "SELECT password_hash FROM fh_cloud.accounts WHERE account_id=$1",
            user.principal.account_id,
        )
        == old_hash
    )
    assert {row["org_id"] for row in await identity.list_organizations(other.principal)} == {
        "org-a",
        "org-b",
    }
    await identity.set_member(
        owner_a.principal, "org-a", user.principal.account_id, "member", False
    )
    with pytest.raises(CloudDenied):
        await identity.authenticate(user.token)
    assert (await identity.authenticate(other.token)).org_id == "org-b"
    assert (await identity.login(user.principal.email, PASSWORD)).principal.org_id == "org-b"


@pytest.mark.parametrize("kind", ["absolute", "idle", "revoked", "disabled"])
async def test_session_expiry_revocation_and_account_current_state(pgcase, identitycase, kind):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    if kind == "absolute":
        await pgcase.admin.execute(
            "UPDATE fh_cloud.sessions SET expires_at=clock_timestamp()-interval '1 second'"
        )
    elif kind == "idle":
        await pgcase.admin.execute(
            "UPDATE fh_cloud.sessions SET last_seen_at=clock_timestamp()-interval '1 hour'"
        )
    elif kind == "revoked":
        await identity.logout(owner.token)
    else:
        await pgcase.admin.execute("UPDATE fh_cloud.accounts SET active=false")
    with pytest.raises(CloudDenied):
        await identity.authenticate(owner.token)


async def test_login_throttle_commits_failures_and_bounds_concurrent_guesses(pgcase, identitycase):
    identity = identitycase.store
    await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    guesses = await asyncio.gather(
        *(identity.login("owner@example.test", "wrong") for _ in range(identity.max_failures)),
        return_exceptions=True,
    )
    assert all(isinstance(result, CloudDenied) for result in guesses)
    with pytest.raises(CloudDenied):
        await identity.login("owner@example.test", PASSWORD)
    assert await pgcase.admin.fetchval("SELECT attempts FROM fh_cloud.login_failures") == 6
    assert [
        row["outcome"]
        for row in await pgcase.admin.fetch(
            "SELECT outcome FROM fh_cloud.authentication_audit WHERE action='login' "
            "ORDER BY created_at"
        )
    ] == ["denied"] * 5 + ["throttled"]
    await pgcase.admin.execute(
        "UPDATE fh_cloud.login_failures SET window_started_at=clock_timestamp()-interval '1 hour'"
    )
    assert (await identity.login("owner@example.test", PASSWORD)).principal.account_id
    assert (
        await pgcase.admin.fetchval(
            "SELECT outcome FROM fh_cloud.authentication_audit WHERE action='login' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        == "allowed"
    )


@pytest.mark.parametrize(
    "email,password",
    [
        ("invalid\ud800@example.test", PASSWORD),
        ("owner@example.test", "invalid\ud800-password"),
    ],
)
async def test_malformed_unicode_credentials_are_denied_and_audited(
    pgcase,
    identitycase,
    email,
    password,
):
    identity = identitycase.store
    await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    with pytest.raises(CloudDenied):
        await identity.login(email, password)
    assert (
        await pgcase.admin.fetchval(
            "SELECT outcome FROM fh_cloud.authentication_audit WHERE action='login'"
        )
        == "denied"
    )


async def test_csrf_organization_switch_and_own_session_rotation(identitycase):
    identity = identitycase.store
    owner_a = await identity.bootstrap("org-a", "owner-a@example.test", PASSWORD)
    owner_b = await identity.bootstrap("org-b", "owner-b@example.test", PASSWORD)
    token = await identity.invite(owner_b.principal, "org-b", owner_a.principal.email)
    await identity.accept_invitation(token, existing_session=owner_a.token)
    for csrf in ("", "x" * 64, owner_b.csrf_token):
        with pytest.raises(CloudDenied):
            await identity.check_csrf(owner_a.token, csrf)
    switched = await identity.switch_organization(owner_a.token, "org-b")
    assert switched.principal.org_id == "org-b"
    assert switched.token != owner_a.token
    with pytest.raises(CloudDenied):
        await identity.authenticate(owner_a.token)
    with pytest.raises(CloudDenied):
        await identity.switch_organization(switched.token, "org-foreign")


async def test_old_principal_cannot_manage_after_revocation_and_last_owner_protection(identitycase):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    admin = await member(identity, owner, "admin@example.test", "admin")
    with pytest.raises(CloudConflict, match="last active"):
        await identity.set_member(
            owner.principal, "org-a", owner.principal.account_id, "member", True
        )
    with pytest.raises(CloudDenied):
        await identity.set_member(
            admin.principal, "org-a", admin.principal.account_id, "owner", True
        )
    await identity.set_member(owner.principal, "org-a", admin.principal.account_id, "member", True)
    with pytest.raises(CloudDenied):
        await identity.put_workspace(admin.principal, "org-a", "ws", "Denied")


async def test_concurrent_owner_removals_preserve_one_without_lock_upgrade_deadlock(identitycase):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    other = await member(identity, owner, "other@example.test", "owner")
    results = await asyncio.wait_for(
        asyncio.gather(
            identity.set_member(
                owner.principal, "org-a", owner.principal.account_id, "member", True
            ),
            identity.set_member(
                other.principal, "org-a", other.principal.account_id, "member", True
            ),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, CloudConflict) for result in results) == 1


async def test_deployment_authorization_lists_and_immutable_binding(pgcase, identitycase):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    user = await member(identity, owner)
    other = await member(identity, owner, "other@example.test")
    await deployed(identity, owner, user)
    assert await identity.list_workspaces(other.principal, "org-a") == []
    assert await identity.list_deployments(other.principal, "org-a", "ws-a") == []
    managed = await identity.list_deployments(owner.principal, "org-a", "ws-a")
    assert len(managed) == 1 and not managed[0]["can_use"] and not managed[0]["can_approve"]
    assert len(await identity.list_workspaces(owner.principal, "org-a")) == 1
    assert len(await identity.list_deployments(user.principal, "org-a", "ws-a")) == 1
    context = TenantContext(org_id="org-a", actor_id=user.principal.actor_id)
    async with pgcase.store.transaction(context) as connection:
        authorized = await authorize_deployment(
            connection, "org-a", context.actor_id, "dep-a", "ws-a"
        )
        assert authorized["binding_digest"] == "a" * 64
        with pytest.raises(CloudDenied):
            await authorize_deployment(connection, "org-a", context.actor_id, "dep-a", "wrong")
    with pytest.raises(CloudConflict):
        await identity.put_deployment(owner.principal, "org-a", "dep-a", "ws-a", "other", "b" * 64)
    await identity.put_deployment(
        owner.principal, "org-a", "dep-a", "ws-a", "synthetic", "a" * 64, active=False
    )
    assert await identity.list_deployments(user.principal, "org-a", "ws-a") == []
    async with pgcase.store.transaction(context) as connection:
        with pytest.raises(CloudDenied):
            await authorize_deployment(connection, "org-a", context.actor_id, "dep-a")
        assert await authorize_deployment(
            connection, "org-a", context.actor_id, "dep-a", historical=True
        )


@pytest.mark.parametrize("kind", ["org", "member", "workspace", "workspace_member", "grant"])
async def test_every_current_authorization_fact_blocks_next_check(pgcase, identitycase, kind):
    identity = identitycase.store
    owner = await identity.bootstrap("org-a", "owner@example.test", PASSWORD)
    user = await member(identity, owner)
    await deployed(identity, owner, user)
    if kind == "org":
        await identity.set_organization(owner.principal, "org-a", False)
    elif kind == "member":
        await identity.set_member(
            owner.principal, "org-a", user.principal.account_id, "member", False
        )
    elif kind == "workspace":
        await identity.put_workspace(owner.principal, "org-a", "ws-a", "Workspace", False)
    elif kind == "workspace_member":
        await identity.set_workspace_member(
            owner.principal, "org-a", "ws-a", user.principal.account_id, False
        )
    else:
        await identity.set_deployment_grant(
            owner.principal, "org-a", "dep-a", user.principal.account_id, active=False
        )
    async with pgcase.store.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('fh.org_id','org-a',true)")
        with pytest.raises(CloudDenied):
            await authorize_deployment(
                connection, "org-a", user.principal.account_id, "dep-a", historical=True
            )


async def test_role_separation_rls_connection_reuse_and_safe_account_columns(pgcase, identitycase):
    identity = identitycase.store
    owner_a = await identity.bootstrap("org-a", "owner-a@example.test", PASSWORD)
    owner_b = await identity.bootstrap("org-b", "owner-b@example.test", PASSWORD)
    await identity.put_workspace(owner_a.principal, "org-a", "ws-a", "A")
    await identity.put_workspace(owner_b.principal, "org-b", "ws-b", "B")
    new_tables = (
        "accounts",
        "org_members",
        "workspaces",
        "workspace_members",
        "deployments",
        "deployment_grants",
        "invitations",
        "sessions",
        "login_failures",
        "identity_audit",
        "authentication_audit",
        "run_bindings",
        "approval_decisions",
    )
    for table in new_tables:
        assert await pgcase.admin.fetchval(
            "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid=$1::regclass",
            "fh_cloud." + table,
        )
    async with pgcase.store.pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SELECT set_config('fh.org_id','org-a',true)")
            assert await connection.fetchval("SELECT count(*) FROM fh_cloud.workspaces") == 1
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM fh_cloud.workspaces WHERE org_id='org-b'"
                )
                == 0
            )
            await connection.execute(
                "SELECT set_config('fh.identity_service','on',true),"
                "set_config('fh.account_id',$1,true)",
                owner_b.principal.account_id,
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM fh_cloud.accounts WHERE account_id=$1",
                    owner_b.principal.account_id,
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM fh_cloud.org_members WHERE org_id='org-b'"
                )
                == 0
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch("SELECT password_hash FROM fh_cloud.accounts")
        async with connection.transaction():
            assert await connection.fetchval("SELECT count(*) FROM fh_cloud.workspaces") == 0
    async with identity.pool.acquire() as connection:
        for statement in (
            "UPDATE fh_cloud.members SET can_execute=true",
            "INSERT INTO fh_cloud.members(org_id,actor_id,can_execute) VALUES('org-a','evil',true)",
            "UPDATE fh_cloud.runs SET status='running'",
            "CREATE TABLE fh_cloud.rogue(x INTEGER)",
            "UPDATE fh_cloud.authentication_audit SET outcome='allowed'",
            "DELETE FROM fh_cloud.authentication_audit",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    async with identitycase.web.pool.acquire() as connection:
        for statement in (
            "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()",
            "UPDATE fh_cloud.runs SET status='running'",
            "UPDATE fh_cloud.execution_checkpoints SET revision=revision+1",
            "UPDATE fh_cloud.invocations SET state='sent'",
            "SELECT * FROM fh_cloud.sessions",
            "SELECT * FROM fh_cloud.authentication_audit",
            "SELECT password_hash FROM fh_cloud.accounts",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)


async def test_schema2_upgrade_is_explicit_preserves_facts_and_inventory(pgcase):
    await pgcase.admin.execute("DROP SCHEMA fh_cloud CASCADE")
    await pgcase.admin.execute(files("forgeharness.cloud").joinpath("schema.sql").read_text())
    await upgrade_1_to_2(pgcase.admin)
    await pgcase.seed()
    await pgcase.admin.execute(
        "INSERT INTO fh_cloud.runs(org_id,run_id,owner_id,request_key,definition,status,"
        "used_units,reserved_units) "
        "VALUES('org-a','old','actor-a','old',$1::jsonb,'reconciling',2,3)",
        definition().model_dump_json(),
    )
    before = dict(await pgcase.admin.fetchrow("SELECT * FROM fh_cloud.runs"))
    inventory = await control_plane_inventory(pgcase.admin)
    assert inventory["active_runs"] == 1
    with pytest.raises(ValueError, match="stopped"):
        await upgrade_2_to_3(pgcase.admin)
    await upgrade_2_to_3(pgcase.admin, writers_stopped=True)
    assert await pgcase.admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 3
    assert dict(await pgcase.admin.fetchrow("SELECT * FROM fh_cloud.runs")) == before
    assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.accounts") == 0
    assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.run_bindings") == 0


async def test_schema2_upgrade_failure_rolls_back_all_ddl(pgcase):
    await pgcase.admin.execute("DROP SCHEMA fh_cloud CASCADE")
    await pgcase.admin.execute(files("forgeharness.cloud").joinpath("schema.sql").read_text())
    await upgrade_1_to_2(pgcase.admin)
    await pgcase.admin.execute("CREATE TABLE fh_cloud.workspaces(sentinel INTEGER)")
    with pytest.raises(asyncpg.DuplicateTableError):
        await upgrade_2_to_3(pgcase.admin, writers_stopped=True)
    assert await pgcase.admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 2
    assert await pgcase.admin.fetchval("SELECT to_regclass('fh_cloud.accounts')") is None
    with pytest.raises(ValueError, match="exact schema version 2"):
        # Temporarily use an impossible version only to exercise precondition;
        # don't rewrite released migration1/2 or a historical execution fact.
        async with pgcase.admin.transaction():
            await pgcase.admin.execute("UPDATE fh_cloud.schema_version SET version=1")
            await upgrade_2_to_3(pgcase.admin, writers_stopped=True)

"""PostgreSQL invitation-only identity adapter for the internal Alpha.

Authentication and tenant authorization are separate contracts. Global identity
transactions can look up credentials, while every tenant operation binds its
own RLS context and rechecks current facts. No raw credential enters the audit.
This module intentionally has no dependency on any reference application.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from forgeharness.cloud.identity_models import Principal, SessionCredentials
from forgeharness.cloud.models import CloudConflict, CloudDenied
from forgeharness.cloud.store import PostgresRunStore

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ROLES = frozenset({"owner", "admin", "member"})
_DENIED = "identity operation unavailable or unauthorized"


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid identity object identifier")
    return value


def _email(value: str) -> str:
    normalized = value.strip().casefold()
    if (
        not 3 <= len(normalized) <= 254
        or normalized.count("@") != 1
        or any(c.isspace() or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in normalized)
        or not all(normalized.split("@"))
    ):
        raise ValueError("invalid email address")
    return normalized


def _password(value: str) -> None:
    if not isinstance(value, str) or not 12 <= len(value) <= 1024:
        raise ValueError("password requires 12-1024 characters")
    try:
        value.encode()
    except UnicodeError as exc:
        raise ValueError("password must contain valid Unicode") from exc


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _csrf(token: str) -> str:
    return _digest("csrf:" + token)


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt:16384:8:1:{salt}:{result.hex()}"


def _verify_password(password: str, encoded: str | None) -> bool:
    # Bounded local adapter parameters are never selected by a credential row.
    try:
        password.encode()
    except UnicodeError:
        _hash_password("", "00" * 16)
        return False
    if encoded is None:
        _hash_password(password, "00" * 16)
        return False
    parts = encoded.split(":")
    if (
        len(parts) != 6
        or parts[:4] != ["scrypt", "16384", "8", "1"]
        or re.fullmatch(r"[a-f0-9]{32}", parts[4]) is None
        or re.fullmatch(r"[a-f0-9]{128}", parts[5]) is None
    ):
        _hash_password(password, "00" * 16)
        return False
    try:
        actual = _hash_password(password, parts[4])
    except ValueError:
        return False
    return hmac.compare_digest(actual, encoded)


async def authorize_deployment(
    connection: Any,
    org_id: str,
    actor_id: str,
    deployment_id: str,
    workspace_id: str | None = None,
    *,
    permission: Literal["use", "approve"] = "use",
    historical: bool = False,
) -> dict[str, Any]:
    """Recheck and lock the current authorization in the caller's PG transaction.

    The caller has already bound ``fh.org_id``. Share locks establish a precise
    order relative to revocation. A physical send already admitted by this
    transaction cannot be recalled by later revocation. Historical reading can
    ignore only deployment retirement; membership and explicit grant still apply.
    """
    if permission not in {"use", "approve"}:
        raise ValueError("unknown deployment permission")
    account = await connection.fetchrow(
        "SELECT account_id,active FROM fh_cloud.accounts WHERE account_id=$1 FOR SHARE",
        actor_id,
    )
    org = await connection.fetchrow(
        "SELECT active FROM fh_cloud.organizations WHERE org_id=$1 FOR SHARE", org_id
    )
    member = await connection.fetchrow(
        "SELECT active FROM fh_cloud.org_members WHERE org_id=$1 AND account_id=$2 FOR SHARE",
        org_id,
        actor_id,
    )
    deployment = await connection.fetchrow(
        "SELECT * FROM fh_cloud.deployments WHERE org_id=$1 AND deployment_id=$2 FOR SHARE",
        org_id,
        deployment_id,
    )
    if (
        account is None
        or not account["active"]
        or org is None
        or not org["active"]
        or member is None
        or not member["active"]
        or deployment is None
        or (not deployment["active"] and not historical)
        or (workspace_id is not None and deployment["workspace_id"] != workspace_id)
    ):
        raise CloudDenied(_DENIED)
    workspace = await connection.fetchrow(
        "SELECT active FROM fh_cloud.workspaces WHERE org_id=$1 AND workspace_id=$2 FOR SHARE",
        org_id,
        deployment["workspace_id"],
    )
    membership = await connection.fetchrow(
        "SELECT active FROM fh_cloud.workspace_members "
        "WHERE org_id=$1 AND workspace_id=$2 AND account_id=$3 FOR SHARE",
        org_id,
        deployment["workspace_id"],
        actor_id,
    )
    grant = await connection.fetchrow(
        "SELECT * FROM fh_cloud.deployment_grants "
        "WHERE org_id=$1 AND deployment_id=$2 AND account_id=$3 FOR SHARE",
        org_id,
        deployment_id,
        actor_id,
    )
    if (
        workspace is None
        or not workspace["active"]
        or membership is None
        or not membership["active"]
        or grant is None
        or not grant["active"]
        or not grant["can_use" if permission == "use" else "can_approve"]
    ):
        raise CloudDenied(_DENIED)
    return dict(deployment)


class PostgresIdentityStore:
    """Credentials and authorization DML using a dedicated least-privilege pool."""

    def __init__(
        self,
        pool: Any,
        *,
        session_ttl_seconds: int = 8 * 3600,
        idle_ttl_seconds: int = 30 * 60,
        invitation_ttl_seconds: int = 3 * 86400,
        max_failures: int = 5,
        failure_window_seconds: int = 15 * 60,
    ) -> None:
        for value in (
            session_ttl_seconds,
            idle_ttl_seconds,
            invitation_ttl_seconds,
            max_failures,
            failure_window_seconds,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("identity limits must be positive integers")
        self.pool = pool
        self.session_ttl_seconds = session_ttl_seconds
        self.idle_ttl_seconds = idle_ttl_seconds
        self.invitation_ttl_seconds = invitation_ttl_seconds
        self.max_failures = max_failures
        self.failure_window_seconds = failure_window_seconds

    @classmethod
    async def connect(cls, dsn: str, **options: Any) -> PostgresIdentityStore:
        asyncpg = cast(Any, importlib.import_module("asyncpg"))
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=30)
        result = cls(pool, **options)
        try:
            async with result._transaction() as connection:
                if await connection.fetchval("SELECT version FROM fh_cloud.schema_version") != 3:
                    raise CloudConflict("identity requires Cloud schema version 3")
                if await connection.fetchval(
                    "SELECT has_table_privilege(current_user,'fh_cloud.runs','UPDATE') "
                    "OR has_column_privilege(current_user,'fh_cloud.members',"
                    "'can_execute','UPDATE')"
                ):
                    raise CloudDenied("identity role must not hold execution privileges")
        except BaseException:
            await pool.close()
            raise
        return result

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Any]:
        async with self.pool.acquire() as connection, connection.transaction():
            await PostgresRunStore._runtime_role(connection)
            await connection.execute("SELECT set_config('fh.identity_service','on',true)")
            yield connection

    @staticmethod
    async def _tenant(connection: Any, org_id: str, account_id: str = "") -> None:
        _identifier(org_id)
        await connection.execute(
            "SELECT set_config('fh.org_id',$1,true),set_config('fh.account_id',$2,true)",
            org_id,
            account_id,
        )

    @staticmethod
    async def _audit(
        connection: Any,
        org_id: str,
        actor_id: str,
        action: str,
        object_id: str,
        outcome: str = "allowed",
    ) -> None:
        await connection.execute(
            "INSERT INTO fh_cloud.identity_audit"
            "(org_id,audit_id,actor_id,action,object_id,outcome) VALUES($1,$2,$3,$4,$5,$6)",
            org_id,
            "audit_" + uuid4().hex,
            actor_id,
            action,
            object_id,
            outcome,
        )

    @staticmethod
    async def _authentication_audit(
        connection: Any,
        key_digest: str,
        action: str,
        outcome: str,
        account_id: str | None = None,
    ) -> None:
        await connection.execute(
            "INSERT INTO fh_cloud.authentication_audit"
            "(audit_id,key_digest,account_id,action,outcome) VALUES($1,$2,$3,$4,$5)",
            "audit_" + uuid4().hex,
            key_digest,
            account_id,
            action,
            outcome,
        )

    async def _session(
        self, connection: Any, *, token: str | None = None, principal: Principal | None = None
    ) -> Principal:
        if token is not None:
            if not isinstance(token, str) or not 20 <= len(token) <= 256:
                raise CloudDenied(_DENIED)
            clause, value = "s.token_digest=$1", _digest(token)
        elif principal is not None:
            clause, value = "s.session_id=$1", principal.session_id
        else:
            raise CloudDenied(_DENIED)
        selected = await connection.fetchrow(
            "SELECT s.org_id FROM fh_cloud.sessions s WHERE " + clause, value
        )
        if selected is None:
            raise CloudDenied(_DENIED)
        if selected["org_id"] is not None:
            await self._tenant(connection, selected["org_id"])
            await connection.fetchrow(
                "SELECT org_id FROM fh_cloud.organizations WHERE org_id=$1 FOR SHARE",
                selected["org_id"],
            )
        row = await connection.fetchrow(
            "SELECT s.*,a.email_normalized,a.active AS account_active "
            "FROM fh_cloud.sessions s JOIN fh_cloud.accounts a USING(account_id) WHERE "
            + clause
            + " FOR UPDATE OF s FOR SHARE OF a",
            value,
        )
        now = await connection.fetchval("SELECT clock_timestamp()")
        if (
            row is None
            or not row["account_active"]
            or row["revoked_at"] is not None
            or row["expires_at"] <= now
            or (now - row["last_seen_at"]).total_seconds() >= self.idle_ttl_seconds
        ):
            raise CloudDenied(_DENIED)
        current = Principal(
            row["account_id"], row["email_normalized"], row["org_id"], row["session_id"]
        )
        if principal is not None and current != principal:
            raise CloudDenied(_DENIED)
        await connection.execute("SELECT set_config('fh.account_id',$1,true)", current.account_id)
        if current.org_id is not None:
            await self._tenant(connection, current.org_id, current.account_id)
            member = await connection.fetchrow(
                "SELECT m.active,o.active AS org_active FROM fh_cloud.org_members m "
                "JOIN fh_cloud.organizations o USING(org_id) "
                "WHERE m.org_id=$1 AND m.account_id=$2 FOR SHARE OF m,o",
                current.org_id,
                current.account_id,
            )
            if member is None or not member["active"] or not member["org_active"]:
                raise CloudDenied(_DENIED)
        await connection.execute(
            "UPDATE fh_cloud.sessions SET last_seen_at=clock_timestamp() WHERE session_id=$1",
            current.session_id,
        )
        return current

    async def _issue(
        self, connection: Any, account_id: str, email: str, org_id: str | None
    ) -> SessionCredentials:
        token = secrets.token_urlsafe(32)
        csrf = _csrf(token)
        session_id = "session_" + uuid4().hex
        expires = await connection.fetchval(
            "INSERT INTO fh_cloud.sessions(session_id,token_digest,csrf_digest,account_id,"
            "org_id,expires_at) VALUES($1,$2,$3,$4,$5,clock_timestamp()"
            "+make_interval(secs=>$6::double precision)) RETURNING expires_at",
            session_id,
            _digest(token),
            _digest(csrf),
            account_id,
            org_id,
            float(self.session_ttl_seconds),
        )
        return SessionCredentials(
            token, csrf, Principal(account_id, email, org_id, session_id), cast(datetime, expires)
        )

    async def _manager(
        self, connection: Any, principal: Principal, org_id: str, *, owner_only: bool = False
    ) -> dict[str, Any]:
        if principal.org_id != org_id:
            raise CloudDenied(_DENIED)
        await self._tenant(connection, org_id, principal.account_id)
        # The organization lock serializes concurrent owner removals and all
        # management mutations, including checks based on an old Principal.
        await connection.fetchrow(
            "SELECT org_id FROM fh_cloud.organizations WHERE org_id=$1 FOR UPDATE", org_id
        )
        current = await self._session(connection, principal=principal)
        member = await connection.fetchrow(
            "SELECT * FROM fh_cloud.org_members WHERE org_id=$1 AND account_id=$2 FOR UPDATE",
            org_id,
            current.account_id,
        )
        if (
            member is None
            or not member["active"]
            or member["role"] not in ({"owner"} if owner_only else {"owner", "admin"})
        ):
            raise CloudDenied(_DENIED)
        return dict(member)

    async def bootstrap(self, org_id: str, email: str, password: str) -> SessionCredentials:
        """Offline-only initial owner; this method is never exposed as an HTTP route."""
        _identifier(org_id)
        normalized = _email(email)
        _password(password)
        encoded = await asyncio.to_thread(_hash_password, password)
        async with self._transaction() as connection:
            await self._tenant(connection, org_id)
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", org_id)
            if await connection.fetchval(
                "SELECT 1 FROM fh_cloud.organizations WHERE org_id=$1", org_id
            ) or await connection.fetchval(
                "SELECT 1 FROM fh_cloud.accounts WHERE email_normalized=$1", normalized
            ):
                raise CloudConflict("bootstrap requires a new organization and account")
            account_id = "acct_" + uuid4().hex
            await connection.execute(
                "INSERT INTO fh_cloud.organizations(org_id) VALUES($1)", org_id
            )
            await connection.execute(
                "INSERT INTO fh_cloud.accounts(account_id,email_normalized,password_hash) "
                "VALUES($1,$2,$3)",
                account_id,
                normalized,
                encoded,
            )
            await connection.execute(
                "INSERT INTO fh_cloud.org_members(org_id,account_id,role) VALUES($1,$2,'owner')",
                org_id,
                account_id,
            )
            await connection.execute(
                "INSERT INTO fh_cloud.members(org_id,actor_id) VALUES($1,$2)", org_id, account_id
            )
            await self._audit(connection, org_id, account_id, "organization.bootstrap", org_id)
            return await self._issue(connection, account_id, normalized, org_id)

    async def login(self, email: str, password: str) -> SessionCredentials:
        try:
            normalized = _email(email)
        except (ValueError, AttributeError):
            normalized = "invalid"
        valid_input = isinstance(password, str) and len(password) <= 1024
        supplied_password = password if valid_input else ""
        result: SessionCredentials | None = None
        async with self._transaction() as connection:
            key = _digest("login:" + normalized)
            await connection.execute(
                "INSERT INTO fh_cloud.login_failures(key_digest,attempts) VALUES($1,0) "
                "ON CONFLICT DO NOTHING",
                key,
            )
            failures = await connection.fetchrow(
                "SELECT * FROM fh_cloud.login_failures WHERE key_digest=$1 FOR UPDATE", key
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            attempts = failures["attempts"]
            if (now - failures["window_started_at"]).total_seconds() >= self.failure_window_seconds:
                attempts = 0
                await connection.execute(
                    "UPDATE fh_cloud.login_failures SET attempts=0,"
                    "window_started_at=clock_timestamp() WHERE key_digest=$1",
                    key,
                )
            account = await connection.fetchrow(
                "SELECT * FROM fh_cloud.accounts WHERE email_normalized=$1 FOR SHARE", normalized
            )
            if attempts < self.max_failures:
                valid = await asyncio.to_thread(
                    _verify_password,
                    supplied_password,
                    account["password_hash"] if account else None,
                )
                if valid_input and valid and account is not None and account["active"]:
                    await connection.execute(
                        "SELECT set_config('fh.account_id',$1,true)", account["account_id"]
                    )
                    orgs = await connection.fetch(
                        "SELECT m.org_id FROM fh_cloud.org_members m "
                        "JOIN fh_cloud.organizations o USING(org_id) "
                        "WHERE m.account_id=$1 AND m.active AND o.active ORDER BY m.org_id",
                        account["account_id"],
                    )
                    org_id = orgs[0]["org_id"] if len(orgs) == 1 else None
                    result = await self._issue(
                        connection, account["account_id"], normalized, org_id
                    )
                    await connection.execute(
                        "UPDATE fh_cloud.login_failures SET attempts=0 WHERE key_digest=$1", key
                    )
            if result is None:
                await connection.execute(
                    "UPDATE fh_cloud.login_failures SET attempts=attempts+1 WHERE key_digest=$1",
                    key,
                )
            await self._authentication_audit(
                connection,
                key,
                "login",
                "allowed"
                if result is not None
                else ("throttled" if attempts >= self.max_failures else "denied"),
                account["account_id"] if account is not None else None,
            )
        # Failed attempts commit; raising inside the transaction would undo the
        # throttle and allow unlimited guesses with concurrent requests.
        if result is None:
            raise CloudDenied(_DENIED)
        return result

    async def authenticate(self, token: str) -> Principal:
        async with self._transaction() as connection:
            return await self._session(connection, token=token)

    async def session_csrf(self, token: str) -> str:
        await self.authenticate(token)
        return _csrf(token)

    async def check_csrf(self, token: str, csrf_token: str) -> Principal:
        if (
            not isinstance(csrf_token, str)
            or len(csrf_token) != 64
            or not hmac.compare_digest(_csrf(token), csrf_token)
        ):
            raise CloudDenied(_DENIED)
        return await self.authenticate(token)

    async def logout(self, token: str) -> None:
        async with self._transaction() as connection:
            principal = await self._session(connection, token=token)
            await connection.execute(
                "UPDATE fh_cloud.sessions SET revoked_at=clock_timestamp() WHERE session_id=$1",
                principal.session_id,
            )
            if principal.org_id:
                await self._audit(
                    connection,
                    principal.org_id,
                    principal.account_id,
                    "session.logout",
                    principal.session_id,
                )

    async def switch_organization(self, token: str, org_id: str) -> SessionCredentials:
        async with self._transaction() as connection:
            principal = await self._session(connection, token=token)
            await self._tenant(connection, org_id, principal.account_id)
            member = await connection.fetchrow(
                "SELECT m.active,o.active AS org_active FROM fh_cloud.org_members m "
                "JOIN fh_cloud.organizations o USING(org_id) "
                "WHERE m.org_id=$1 AND m.account_id=$2 FOR SHARE OF m,o",
                org_id,
                principal.account_id,
            )
            if member is None or not member["active"] or not member["org_active"]:
                raise CloudDenied(_DENIED)
            await connection.execute(
                "UPDATE fh_cloud.sessions SET revoked_at=clock_timestamp() WHERE session_id=$1",
                principal.session_id,
            )
            await self._audit(connection, org_id, principal.account_id, "session.switch", org_id)
            return await self._issue(connection, principal.account_id, principal.email, org_id)

    async def invite(
        self, principal: Principal, org_id: str, email: str, role: str = "member"
    ) -> str:
        if role not in _ROLES:
            raise ValueError("unknown organization role")
        normalized = _email(email)
        token = secrets.token_urlsafe(32)
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id, owner_only=role == "owner")
            invitation_id = "invite_" + uuid4().hex
            await connection.execute(
                "INSERT INTO fh_cloud.invitations(org_id,invitation_id,token_digest,"
                "email_normalized,role,invited_by,expires_at) VALUES($1,$2,$3,$4,$5,$6,"
                "clock_timestamp()+make_interval(secs=>$7::double precision))",
                org_id,
                invitation_id,
                _digest(token),
                normalized,
                role,
                principal.account_id,
                float(self.invitation_ttl_seconds),
            )
            await self._audit(
                connection, org_id, principal.account_id, "invitation.create", invitation_id
            )
        return token

    async def revoke_invitation(
        self, principal: Principal, org_id: str, invitation_id: str
    ) -> None:
        async with self._transaction() as connection:
            actor = await self._manager(connection, principal, org_id)
            row = await connection.fetchrow(
                "SELECT role,consumed_at FROM fh_cloud.invitations "
                "WHERE org_id=$1 AND invitation_id=$2 FOR UPDATE",
                org_id,
                invitation_id,
            )
            if row is None or (row["role"] == "owner" and actor["role"] != "owner"):
                raise CloudDenied(_DENIED)
            if row["consumed_at"] is not None:
                raise CloudConflict("a consumed invitation cannot be revoked")
            await connection.execute(
                "UPDATE fh_cloud.invitations SET revoked_at=COALESCE(revoked_at,clock_timestamp()) "
                "WHERE org_id=$1 AND invitation_id=$2",
                org_id,
                invitation_id,
            )
            await self._audit(
                connection, org_id, principal.account_id, "invitation.revoke", invitation_id
            )

    async def list_invitations(
        self, principal: Principal, org_id: str, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invitation page size must be 1-100")
        if cursor is not None and re.fullmatch(r"invite_[a-f0-9]{32}", cursor) is None:
            raise ValueError("invalid invitation cursor")
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id)
            rows = await connection.fetch(
                "SELECT invitation_id,email_normalized AS email,role,expires_at,consumed_at,"
                "revoked_at FROM fh_cloud.invitations WHERE org_id=$1 "
                "AND ($2::text IS NULL OR invitation_id>$2) ORDER BY invitation_id LIMIT $3",
                org_id,
                cursor,
                limit + 1,
            )
            return {
                "items": [dict(row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["invitation_id"] if len(rows) > limit else None,
            }

    async def accept_invitation(
        self, token: str, password: str | None = None, existing_session: str | None = None
    ) -> SessionCredentials:
        key = _digest("invitation:" + (token if isinstance(token, str) else "invalid"))
        try:
            return await self._accept_invitation(token, password, existing_session)
        except (CloudDenied, CloudConflict, ValueError):
            # The failed acceptance transaction rolls back first; its audit
            # commits independently before the normalized failure is returned.
            async with self._transaction() as connection:
                await self._authentication_audit(connection, key, "invitation_accept", "denied")
            raise

    async def _accept_invitation(
        self, token: str, password: str | None = None, existing_session: str | None = None
    ) -> SessionCredentials:
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            raise CloudDenied(_DENIED)
        async with self._transaction() as connection:
            selected = await connection.fetchrow(
                "SELECT org_id FROM fh_cloud.invitations WHERE token_digest=$1",
                _digest(token),
            )
            if selected is None:
                raise CloudDenied(_DENIED)
            org_ids = {selected["org_id"]}
            if existing_session is not None:
                source_org = await connection.fetchval(
                    "SELECT org_id FROM fh_cloud.sessions WHERE token_digest=$1",
                    _digest(existing_session),
                )
                if source_org is not None:
                    org_ids.add(source_org)
            # Lock both organizations in a stable order before touching session,
            # member or invitation rows. Cross-org acceptance cannot deadlock
            # with reciprocal invitations or concurrent admin revocation.
            for locked_org in sorted(org_ids):
                await self._tenant(connection, locked_org)
                await connection.fetchrow(
                    "SELECT org_id FROM fh_cloud.organizations WHERE org_id=$1 FOR UPDATE",
                    locked_org,
                )
            invitation = await connection.fetchrow(
                "SELECT * FROM fh_cloud.invitations WHERE token_digest=$1 FOR UPDATE",
                _digest(token),
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            if (
                invitation is None
                or invitation["consumed_at"] is not None
                or invitation["revoked_at"] is not None
                or invitation["expires_at"] <= now
            ):
                raise CloudDenied(_DENIED)
            email = invitation["email_normalized"]
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,1))", email)
            account = await connection.fetchrow(
                "SELECT * FROM fh_cloud.accounts WHERE email_normalized=$1 FOR SHARE", email
            )
            if account is not None:
                if existing_session is None or password is not None:
                    raise CloudDenied(_DENIED)
                current = await self._session(connection, token=existing_session)
                if current.account_id != account["account_id"]:
                    raise CloudDenied(_DENIED)
                account_id = current.account_id
            else:
                if password is None or existing_session is not None:
                    raise CloudDenied(_DENIED)
                _password(password)
                encoded = await asyncio.to_thread(_hash_password, password)
                account_id = "acct_" + uuid4().hex
                await connection.execute(
                    "INSERT INTO fh_cloud.accounts(account_id,email_normalized,password_hash) "
                    "VALUES($1,$2,$3)",
                    account_id,
                    email,
                    encoded,
                )
            org_id = invitation["org_id"]
            await self._tenant(connection, org_id, account_id)
            issuer = await connection.fetchrow(
                "SELECT m.active,m.role,o.active AS org_active FROM fh_cloud.org_members m "
                "JOIN fh_cloud.organizations o USING(org_id) "
                "WHERE m.org_id=$1 AND m.account_id=$2 FOR SHARE OF m,o",
                org_id,
                invitation["invited_by"],
            )
            if (
                issuer is None
                or not issuer["active"]
                or not issuer["org_active"]
                or issuer["role"] not in {"owner", "admin"}
                or (invitation["role"] == "owner" and issuer["role"] != "owner")
            ):
                raise CloudDenied(_DENIED)
            previous = await connection.fetchrow(
                "SELECT active FROM fh_cloud.org_members WHERE org_id=$1 AND account_id=$2 "
                "FOR UPDATE",
                org_id,
                account_id,
            )
            if previous is not None and previous["active"]:
                raise CloudConflict("account is already an organization member")
            await connection.execute(
                "INSERT INTO fh_cloud.org_members(org_id,account_id,role) VALUES($1,$2,$3) "
                "ON CONFLICT(org_id,account_id) DO UPDATE SET active=true,role=EXCLUDED.role,"
                "version=fh_cloud.org_members.version+1",
                org_id,
                account_id,
                invitation["role"],
            )
            await connection.execute(
                "INSERT INTO fh_cloud.members(org_id,actor_id) VALUES($1,$2) "
                "ON CONFLICT(org_id,actor_id) DO UPDATE SET active=true",
                org_id,
                account_id,
            )
            await connection.execute(
                "UPDATE fh_cloud.invitations SET consumed_at=clock_timestamp() "
                "WHERE token_digest=$1",
                _digest(token),
            )
            await self._audit(
                connection, org_id, account_id, "invitation.accept", invitation["invitation_id"]
            )
            await self._authentication_audit(
                connection,
                _digest("invitation:" + token),
                "invitation_accept",
                "allowed",
                account_id,
            )
            return await self._issue(connection, account_id, email, org_id)

    async def list_organizations(self, principal: Principal) -> list[dict[str, Any]]:
        async with self._transaction() as connection:
            await self._session(connection, principal=principal)
            rows = await connection.fetch(
                "SELECT m.org_id,m.role FROM fh_cloud.org_members m "
                "JOIN fh_cloud.organizations o USING(org_id) WHERE m.account_id=$1 "
                "AND m.active AND o.active ORDER BY m.org_id LIMIT 100",
                principal.account_id,
            )
            return [dict(row) for row in rows]

    async def list_members(self, principal: Principal, org_id: str) -> list[dict[str, Any]]:
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id)
            rows = await connection.fetch(
                "SELECT m.account_id,a.email_normalized AS email,m.role,m.active,m.version "
                "FROM fh_cloud.org_members m JOIN fh_cloud.accounts a USING(account_id) "
                "WHERE m.org_id=$1 ORDER BY m.account_id LIMIT 100",
                org_id,
            )
            return [dict(row) for row in rows]

    async def set_member(
        self, principal: Principal, org_id: str, account_id: str, role: str, active: bool
    ) -> None:
        if role not in _ROLES or type(active) is not bool:
            raise ValueError("invalid member role or active flag")
        async with self._transaction() as connection:
            actor = await self._manager(connection, principal, org_id)
            target = await connection.fetchrow(
                "SELECT * FROM fh_cloud.org_members WHERE org_id=$1 AND account_id=$2 FOR UPDATE",
                org_id,
                account_id,
            )
            if target is None:
                raise CloudDenied(_DENIED)
            if (target["role"] == "owner" or role == "owner") and actor["role"] != "owner":
                raise CloudDenied(_DENIED)
            if target["role"] == "owner" and target["active"] and (role != "owner" or not active):
                owners = await connection.fetchval(
                    "SELECT count(*) FROM fh_cloud.org_members "
                    "WHERE org_id=$1 AND role='owner' AND active",
                    org_id,
                )
                if owners <= 1:
                    raise CloudConflict("the last active organization owner must remain")
            await connection.execute(
                "UPDATE fh_cloud.org_members SET role=$3,active=$4,version=version+1 "
                "WHERE org_id=$1 AND account_id=$2",
                org_id,
                account_id,
                role,
                active,
            )
            await connection.execute(
                "UPDATE fh_cloud.members SET active=$3 WHERE org_id=$1 AND actor_id=$2",
                org_id,
                account_id,
                active,
            )
            # Revoking one organization does not revoke this account's sessions
            # in other organizations or reset any global credential.
            if not active:
                await connection.execute(
                    "UPDATE fh_cloud.sessions SET revoked_at=clock_timestamp() "
                    "WHERE org_id=$1 AND account_id=$2 AND revoked_at IS NULL",
                    org_id,
                    account_id,
                )
            await self._audit(connection, org_id, principal.account_id, "member.set", account_id)

    async def set_organization(self, principal: Principal, org_id: str, active: bool) -> None:
        if type(active) is not bool:
            raise ValueError("active must be boolean")
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id, owner_only=True)
            await connection.execute(
                "UPDATE fh_cloud.organizations SET active=$2 WHERE org_id=$1", org_id, active
            )
            await self._audit(connection, org_id, principal.account_id, "organization.set", org_id)

    async def put_workspace(
        self, principal: Principal, org_id: str, workspace_id: str, name: str, active: bool = True
    ) -> None:
        _identifier(workspace_id)
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 200
            or type(active) is not bool
        ):
            raise ValueError("invalid workspace name or active flag")
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id)
            await connection.execute(
                "INSERT INTO fh_cloud.workspaces(org_id,workspace_id,name,active) "
                "VALUES($1,$2,$3,$4) ON CONFLICT(org_id,workspace_id) "
                "DO UPDATE SET name=EXCLUDED.name,active=EXCLUDED.active",
                org_id,
                workspace_id,
                name.strip(),
                active,
            )
            await self._audit(
                connection, org_id, principal.account_id, "workspace.set", workspace_id
            )

    async def set_workspace_member(
        self,
        principal: Principal,
        org_id: str,
        workspace_id: str,
        account_id: str,
        active: bool = True,
    ) -> None:
        if type(active) is not bool:
            raise ValueError("active must be boolean")
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id)
            if not await connection.fetchval(
                "SELECT 1 FROM fh_cloud.org_members WHERE org_id=$1 AND account_id=$2",
                org_id,
                account_id,
            ) or not await connection.fetchval(
                "SELECT 1 FROM fh_cloud.workspaces WHERE org_id=$1 AND workspace_id=$2",
                org_id,
                workspace_id,
            ):
                raise CloudDenied(_DENIED)
            await connection.execute(
                "INSERT INTO fh_cloud.workspace_members(org_id,workspace_id,account_id,active) "
                "VALUES($1,$2,$3,$4) ON CONFLICT(org_id,workspace_id,account_id) DO UPDATE "
                "SET active=EXCLUDED.active",
                org_id,
                workspace_id,
                account_id,
                active,
            )
            await self._audit(
                connection,
                org_id,
                principal.account_id,
                "workspace.member_set",
                workspace_id + ":" + account_id,
            )

    async def put_deployment(
        self,
        principal: Principal,
        org_id: str,
        deployment_id: str,
        workspace_id: str,
        application_id: str,
        binding_digest: str,
        active: bool = True,
    ) -> None:
        _identifier(deployment_id)
        _identifier(workspace_id)
        _identifier(application_id)
        if re.fullmatch(r"[a-f0-9]{64}", binding_digest) is None or type(active) is not bool:
            raise ValueError("invalid immutable deployment binding")
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id)
            previous = await connection.fetchrow(
                "SELECT * FROM fh_cloud.deployments WHERE org_id=$1 "
                "AND deployment_id=$2 FOR UPDATE",
                org_id,
                deployment_id,
            )
            if previous is not None and (
                previous["workspace_id"] != workspace_id
                or previous["application_id"] != application_id
                or previous["binding_digest"] != binding_digest
            ):
                raise CloudConflict("deployment workspace and release binding are immutable")
            if not await connection.fetchval(
                "SELECT 1 FROM fh_cloud.workspaces WHERE org_id=$1 AND workspace_id=$2",
                org_id,
                workspace_id,
            ):
                raise CloudDenied(_DENIED)
            await connection.execute(
                "INSERT INTO fh_cloud.deployments(org_id,deployment_id,workspace_id,application_id,"
                "binding_digest,active) VALUES($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT(org_id,deployment_id) "
                "DO UPDATE SET active=EXCLUDED.active",
                org_id,
                deployment_id,
                workspace_id,
                application_id,
                binding_digest,
                active,
            )
            await self._audit(
                connection, org_id, principal.account_id, "deployment.set", deployment_id
            )

    async def set_deployment_grant(
        self,
        principal: Principal,
        org_id: str,
        deployment_id: str,
        account_id: str,
        can_use: bool = True,
        can_approve: bool = False,
        active: bool = True,
    ) -> None:
        if any(type(value) is not bool for value in (can_use, can_approve, active)):
            raise ValueError("grant flags must be boolean")
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id)
            if not await connection.fetchval(
                "SELECT 1 FROM fh_cloud.org_members WHERE org_id=$1 AND account_id=$2",
                org_id,
                account_id,
            ) or not await connection.fetchval(
                "SELECT 1 FROM fh_cloud.deployments WHERE org_id=$1 AND deployment_id=$2",
                org_id,
                deployment_id,
            ):
                raise CloudDenied(_DENIED)
            await connection.execute(
                "INSERT INTO fh_cloud.deployment_grants(org_id,deployment_id,account_id,can_use,"
                "can_approve,active) VALUES($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT(org_id,deployment_id,account_id) DO UPDATE SET "
                "can_use=EXCLUDED.can_use,can_approve=EXCLUDED.can_approve,active=EXCLUDED.active",
                org_id,
                deployment_id,
                account_id,
                can_use,
                can_approve,
                active,
            )
            await connection.execute(
                "UPDATE fh_cloud.members SET can_approve=EXISTS(SELECT 1 FROM "
                "fh_cloud.deployment_grants g WHERE g.org_id=$1 AND g.account_id=$2 "
                "AND g.active AND g.can_approve) WHERE org_id=$1 AND actor_id=$2",
                org_id,
                account_id,
            )
            await self._audit(
                connection,
                org_id,
                principal.account_id,
                "deployment.grant_set",
                deployment_id + ":" + account_id,
            )

    async def list_workspaces(self, principal: Principal, org_id: str) -> list[dict[str, Any]]:
        async with self._transaction() as connection:
            current = await self._session(connection, principal=principal)
            if current.org_id != org_id:
                raise CloudDenied(_DENIED)
            rows = await connection.fetch(
                "SELECT w.workspace_id,w.name,w.active FROM fh_cloud.workspaces w "
                "LEFT JOIN fh_cloud.workspace_members m ON m.org_id=w.org_id "
                "AND m.workspace_id=w.workspace_id AND m.account_id=$2 "
                "JOIN fh_cloud.org_members om ON om.org_id=w.org_id AND om.account_id=$2 "
                "WHERE w.org_id=$1 AND ((w.active AND m.active) "
                "OR om.role IN ('owner','admin')) "
                "ORDER BY w.workspace_id LIMIT 100",
                org_id,
                principal.account_id,
            )
            return [dict(row) for row in rows]

    async def list_deployments(
        self, principal: Principal, org_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        async with self._transaction() as connection:
            current = await self._session(connection, principal=principal)
            if current.org_id != org_id:
                raise CloudDenied(_DENIED)
            rows = await connection.fetch(
                "SELECT d.deployment_id,d.application_id,d.active,"
                "COALESCE(d.active AND w.active AND m.active AND g.active AND g.can_use,false) "
                "AS can_use,COALESCE(d.active AND w.active AND m.active AND g.active "
                "AND g.can_approve,false) AS can_approve "
                "FROM fh_cloud.deployments d "
                "JOIN fh_cloud.workspaces w USING(org_id,workspace_id) "
                "LEFT JOIN fh_cloud.deployment_grants g ON g.org_id=d.org_id "
                "AND g.deployment_id=d.deployment_id AND g.account_id=$3 "
                "LEFT JOIN fh_cloud.workspace_members m ON m.org_id=d.org_id "
                "AND m.workspace_id=d.workspace_id AND m.account_id=$3 "
                "JOIN fh_cloud.org_members om ON om.org_id=d.org_id AND om.account_id=$3 "
                "WHERE d.org_id=$1 AND d.workspace_id=$2 AND ((d.active AND w.active "
                "AND m.active AND g.active AND (g.can_use OR g.can_approve)) "
                "OR om.role IN ('owner','admin')) "
                "ORDER BY d.deployment_id LIMIT 100",
                org_id,
                workspace_id,
                principal.account_id,
            )
            return [dict(row) for row in rows]

    async def audit_tail(
        self, principal: Principal, org_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("audit page size must be 1-100")
        async with self._transaction() as connection:
            await self._manager(connection, principal, org_id)
            rows = await connection.fetch(
                "SELECT audit_id,actor_id,action,object_id,outcome,created_at "
                "FROM fh_cloud.identity_audit WHERE org_id=$1 "
                "ORDER BY created_at DESC,audit_id DESC LIMIT $2",
                org_id,
                limit,
            )
            return [dict(row) for row in rows]

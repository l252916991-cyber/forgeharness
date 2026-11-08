"""Invitation-only accounts, workspace membership and current authorization.

The SDK trusts the ``Actor`` a host supplies, so this module is the whole identity
boundary for the commerce workbench: a browser credential is verified here, the
membership row is re-read on every request, and the ``RunContext`` scope is derived
from those rows. Request bodies, path parameters, headers and model output never
name an actor.

Design notes that the tests pin:

* ``users.id`` is a random identifier and reaches the harness as ``user:<id>``, so two
  people holding the same role never collapse into one actor.
* Only digests of browser and approval tokens are stored. Passwords use ``scrypt``
  with a per-user salt and are compared in constant time.
* Disabling a user or revoking a membership takes effect on the next request: every
  authentication re-reads the account row and the membership rows.
* An admin holds neither ``approval:decide`` nor ``consult``, so administration is not
  a shortcut around business approval.
* A consultation session identifier is bound to exactly one user per workspace; an
  identifier is never an authorization credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from forgeharness.sdk import Actor, ResourceScope, RunContext, ScopeError

COOKIE_NAME = "fh_identity"
CSRF_HEADER = "x-csrf-token"
APPROVAL_HEADER = "x-approval-token"
SESSION_HEADER = "x-session-id"

ROLES: tuple[str, ...] = (
    "admin",
    "customer",
    "editor",
    "publisher",
    "supervisor",
    "support",
)

# Role -> permission grants. This table, not a document, is what the application
# enforces; ``reports/application-20261028/permission-matrix.json`` mirrors it.
PERMISSIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "customer": frozenset({"consult", "ticket:read_own"}),
        "support": frozenset({"ticket:read_scope", "ticket:claim", "ticket:resolve"}),
        "supervisor": frozenset({"ticket:read_scope", "approval:decide"}),
        "editor": frozenset({"knowledge:read", "knowledge:draft"}),
        "publisher": frozenset({"knowledge:read", "knowledge:review", "knowledge:publish"}),
        # An administrator operates the service. It deliberately holds neither
        # ``approval:decide`` nor ``consult``: administration is never a business
        # approval and never a customer identity.
        "admin": frozenset({"member:manage", "quota:manage", "knowledge:read"}),
    }
)

PASSWORD_MIN_LENGTH = 12
SESSION_TTL = timedelta(hours=8)
APPROVAL_TTL = timedelta(minutes=10)
INVITATION_TTL = timedelta(days=3)
FAILURE_WINDOW = timedelta(minutes=15)
MAX_FAILURES = 5

_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_ACTOR_PREFIX = "user:"
_INVALID_CREDENTIALS = "credentials are not valid for this workspace"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id, role)
);
CREATE TABLE IF NOT EXISTS invitations (
    token_hash TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    invited_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    csrf TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS approval_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS consultation_sessions (
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (workspace_id, session_id)
);
CREATE TABLE IF NOT EXISTS login_failures (
    email TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    workspace_id TEXT,
    actor_id TEXT,
    action TEXT NOT NULL,
    object_id TEXT,
    outcome TEXT NOT NULL,
    detail TEXT
);
"""


class AuthenticationError(ScopeError):
    """A credential, membership or account state did not authorize this request."""


class LoginThrottled(AuthenticationError):
    """Too many failed attempts for one address inside the failure window."""

    def __init__(self) -> None:
        super().__init__("too many failed attempts; retry later")


class RegistrationError(ValueError):
    """An account or invitation cannot be created as requested."""


@dataclass(frozen=True, slots=True)
class Principal:
    """A currently valid identity inside exactly one workspace."""

    user_id: str
    display_name: str
    workspace_id: str
    roles: tuple[str, ...]

    @property
    def actor_id(self) -> str:
        """Stable harness actor id; deliberately not a role name."""
        return f"{_ACTOR_PREFIX}{self.user_id}"

    @property
    def actor(self) -> Actor:
        return Actor(id=self.actor_id, roles=self.roles)

    @property
    def permissions(self) -> frozenset[str]:
        granted: set[str] = set()
        for role in self.roles:
            granted |= PERMISSIONS[role]
        return frozenset(granted)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if not self.has(permission):
            raise ScopeError(f"{permission} is not granted for this identity")


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A newly issued browser credential; the raw token is returned once."""

    token: str
    csrf: str
    expires_at: datetime
    principal: Principal


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """A short-lived credential for a separately authenticated approver."""

    token: str
    expires_at: datetime
    principal: Principal


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not 3 <= len(normalized) <= 254 or normalized.count("@") != 1:
        raise RegistrationError("a valid email address is required")
    local, _, domain = normalized.partition("@")
    if not local or "." not in domain:
        raise RegistrationError("a valid email address is required")
    return normalized


def _validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise RegistrationError(f"password needs at least {PASSWORD_MIN_LENGTH} characters")


def _new_salt() -> str:
    return secrets.token_hex(16)


def _digest_password(password: str, salt: str) -> str:
    derived = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return derived.hex()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _validate_session_id(session_id: str) -> None:
    if not 1 <= len(session_id) <= 200:
        raise ScopeError("session binding is outside the authenticated identity")
    if any(character in session_id for character in ("/", "\\", "\x00")):
        raise ScopeError("session binding is outside the authenticated identity")


class IdentityStore:
    """Invitation-only accounts, membership and per-request authorization.

    ``now`` is injectable so expiry and throttling are testable without sleeping.
    """

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
        session_ttl: timedelta = SESSION_TTL,
        approval_ttl: timedelta = APPROVAL_TTL,
        invitation_ttl: timedelta = INVITATION_TTL,
        failure_window: timedelta = FAILURE_WINDOW,
        max_failures: int = MAX_FAILURES,
    ) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(UTC))
        self._session_ttl = session_ttl
        self._approval_ttl = approval_ttl
        self._invitation_ttl = invitation_ttl
        self._failure_window = failure_window
        self._max_failures = max_failures
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    # -- connection and auditing -------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        at: datetime,
        action: str,
        outcome: str,
        actor_id: str | None = None,
        workspace_id: str | None = None,
        object_id: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """Record an authorization-relevant action; never includes a secret."""
        connection.execute(
            "INSERT INTO audit(at, workspace_id, actor_id, action, object_id, outcome, detail) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                _iso(at),
                workspace_id,
                actor_id,
                action,
                object_id,
                outcome,
                json.dumps(dict(detail), sort_keys=True) if detail else None,
            ),
        )

    def audit_tail(self, *, limit: int = 50) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT at, workspace_id, actor_id, action, object_id, outcome, detail "
                "FROM audit ORDER BY seq DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    # -- workspaces, accounts and memberships ------------------------------------

    def create_workspace(self, name: str, *, workspace_id: str | None = None) -> str:
        """Create a workspace. ``workspace_id`` is only for matching an existing host tenancy."""
        if not name.strip():
            raise RegistrationError("a workspace needs a name")
        workspace_id = workspace_id or f"ws-{uuid.uuid4().hex[:16]}"
        if not 1 <= len(workspace_id) <= 128:
            raise RegistrationError("a workspace id needs 1-128 characters")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO workspaces(id, name, status, created_at) VALUES(?,?,?,?)",
                (workspace_id, name.strip(), "active", _iso(self._now())),
            )
        return workspace_id

    def add_user(
        self,
        *,
        workspace_id: str,
        email: str,
        password: str,
        display_name: str,
        role: str,
    ) -> str:
        """Create an account and its first membership (bootstrap and invitations)."""
        normalized = _normalize_email(email)
        _validate_password(password)
        self._validate_role(role)
        if not display_name.strip():
            raise RegistrationError("a display name is required")
        user_id = f"usr-{uuid.uuid4().hex}"
        salt = _new_salt()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
                ).fetchone()
                is None
            ):
                raise RegistrationError("unknown workspace")
            if (
                connection.execute("SELECT 1 FROM users WHERE email=?", (normalized,)).fetchone()
                is not None
            ):
                raise RegistrationError("an account for this email already exists")
            connection.execute(
                "INSERT INTO users(id, email, display_name, status, password_salt, "
                "password_hash, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    user_id,
                    normalized,
                    display_name.strip(),
                    "active",
                    salt,
                    _digest_password(password, salt),
                    _iso(self._now()),
                ),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) "
                "VALUES(?,?,?,1,?)",
                (workspace_id, user_id, role, _iso(self._now())),
            )
            self._audit(
                connection,
                at=self._now(),
                action="member.add",
                outcome="allowed",
                actor_id=None,
                workspace_id=workspace_id,
                object_id=user_id,
                detail={"role": role},
            )
        return user_id

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in ROLES:
            raise RegistrationError(f"unknown role: {role}")

    def invite(
        self,
        *,
        actor: Principal,
        email: str,
        role: str,
        ttl: timedelta | None = None,
    ) -> str:
        """Create an invitation for the actor's own workspace; returns the raw token."""
        normalized = _normalize_email(email)
        self._validate_role(role)
        expires_at = self._now() + (ttl or self._invitation_ttl)
        token = _new_token()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor = self._principal(
                connection, user_id=actor.user_id, workspace_id=actor.workspace_id
            )
            actor.require("member:manage")
            existing = connection.execute(
                "SELECT 1 FROM users WHERE email=?", (normalized,)
            ).fetchone()
            if existing is not None:
                raise RegistrationError("an account for this email already exists")
            connection.execute(
                "INSERT INTO invitations(token_hash, workspace_id, email, role, invited_by, "
                "created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    _token_digest(token),
                    actor.workspace_id,
                    normalized,
                    role,
                    actor.actor_id,
                    _iso(self._now()),
                    _iso(expires_at),
                ),
            )
            self._audit(
                connection,
                at=self._now(),
                action="invitation.create",
                outcome="allowed",
                actor_id=actor.actor_id,
                workspace_id=actor.workspace_id,
                detail={"role": role},
            )
        return token

    def accept_invitation(self, *, token: str, password: str, display_name: str) -> LoginResult:
        """Consume an invitation and create the account with the invited role only."""
        _validate_password(password)
        if not display_name.strip():
            raise RegistrationError("a display name is required")
        digest = _token_digest(token)
        now = self._now()
        user_id = f"usr-{uuid.uuid4().hex}"
        salt = _new_salt()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM invitations WHERE token_hash=?", (digest,)
            ).fetchone()
            if (
                row is None
                or row["accepted_at"] is not None
                or row["revoked_at"] is not None
                or _moment(str(row["expires_at"])) <= now
            ):
                raise AuthenticationError("invitation is not valid")
            workspace_id = str(row["workspace_id"])
            role = str(row["role"])
            email = str(row["email"])
            if (
                connection.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
                is not None
            ):
                raise RegistrationError("an account for this email already exists")
            connection.execute(
                "INSERT INTO users(id, email, display_name, status, password_salt, "
                "password_hash, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    user_id,
                    email,
                    display_name.strip(),
                    "active",
                    salt,
                    _digest_password(password, salt),
                    _iso(now),
                ),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) "
                "VALUES(?,?,?,1,?)",
                (workspace_id, user_id, role, _iso(now)),
            )
            connection.execute(
                "UPDATE invitations SET accepted_at=? WHERE token_hash=?",
                (_iso(now), digest),
            )
            self._audit(
                connection,
                at=now,
                action="invitation.accept",
                outcome="allowed",
                actor_id=f"{_ACTOR_PREFIX}{user_id}",
                workspace_id=workspace_id,
                object_id=user_id,
                detail={"role": role},
            )
            return self._issue_web_session(
                connection,
                principal=Principal(
                    user_id=user_id,
                    display_name=display_name.strip(),
                    workspace_id=workspace_id,
                    roles=(role,),
                ),
                now=now,
                action="invitation.accept",
            )

    def disable_user(self, *, actor: Principal, user_id: str) -> None:
        """Disable this workspace's membership and credentials, never another tenancy.

        Accounts are global, but a workspace administrator's authority is local.
        Global account suspension is not exposed through this method.
        """
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor = self._principal(
                connection, user_id=actor.user_id, workspace_id=actor.workspace_id
            )
            actor.require("member:manage")
            if actor.user_id == user_id:
                raise ScopeError("an administrator cannot disable its own account")
            row = connection.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?",
                (actor.workspace_id, user_id),
            ).fetchone()
            if row is None:
                raise ScopeError("member is outside this workspace")
            connection.execute(
                "UPDATE memberships SET active=0 WHERE workspace_id=? AND user_id=?",
                (actor.workspace_id, user_id),
            )
            self._revoke_credentials(
                connection, user_id=user_id, workspace_id=actor.workspace_id, at=now
            )
            self._audit(
                connection,
                at=now,
                action="member.disable",
                outcome="allowed",
                actor_id=actor.actor_id,
                workspace_id=actor.workspace_id,
                object_id=user_id,
            )

    def grant_role(self, *, actor: Principal, user_id: str, role: str) -> None:
        """Add or re-activate one role inside the actor's own workspace."""
        self._validate_role(role)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor = self._principal(
                connection, user_id=actor.user_id, workspace_id=actor.workspace_id
            )
            actor.require("member:manage")
            if (
                connection.execute(
                    "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?",
                    (actor.workspace_id, user_id),
                ).fetchone()
                is None
            ):
                raise ScopeError("member is outside this workspace")
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) "
                "VALUES(?,?,?,1,?) ON CONFLICT(workspace_id, user_id, role) "
                "DO UPDATE SET active=1",
                (actor.workspace_id, user_id, role, _iso(now)),
            )
            self._audit(
                connection,
                at=now,
                action="member.grant",
                outcome="allowed",
                actor_id=actor.actor_id,
                workspace_id=actor.workspace_id,
                object_id=user_id,
                detail={"role": role},
            )

    def revoke_membership(self, *, actor: Principal, user_id: str, role: str) -> None:
        """Remove one role, then revoke credentials so the change is immediate."""
        self._validate_role(role)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor = self._principal(
                connection, user_id=actor.user_id, workspace_id=actor.workspace_id
            )
            actor.require("member:manage")
            if actor.user_id == user_id:
                raise ScopeError("an administrator cannot revoke its own membership")
            row = connection.execute(
                "SELECT active FROM memberships WHERE workspace_id=? AND user_id=? AND role=?",
                (actor.workspace_id, user_id, role),
            ).fetchone()
            if row is None:
                raise ScopeError("membership is outside this workspace")
            connection.execute(
                "UPDATE memberships SET active=0 WHERE workspace_id=? AND user_id=? AND role=?",
                (actor.workspace_id, user_id, role),
            )
            self._revoke_credentials(
                connection, user_id=user_id, workspace_id=actor.workspace_id, at=now
            )
            self._audit(
                connection,
                at=now,
                action="member.revoke",
                outcome="allowed",
                actor_id=actor.actor_id,
                workspace_id=actor.workspace_id,
                object_id=user_id,
                detail={"role": role},
            )

    @staticmethod
    def _revoke_credentials(
        connection: sqlite3.Connection, *, user_id: str, workspace_id: str, at: datetime
    ) -> None:
        connection.execute(
            "UPDATE web_sessions SET revoked_at=? "
            "WHERE user_id=? AND workspace_id=? AND revoked_at IS NULL",
            (_iso(at), user_id, workspace_id),
        )
        connection.execute(
            "UPDATE approval_sessions SET revoked_at=? "
            "WHERE user_id=? AND workspace_id=? AND revoked_at IS NULL",
            (_iso(at), user_id, workspace_id),
        )

    # -- credentials -------------------------------------------------------------

    def _roles(
        self, connection: sqlite3.Connection, *, workspace_id: str, user_id: str
    ) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT role FROM memberships WHERE workspace_id=? AND user_id=? AND active=1",
            (workspace_id, user_id),
        ).fetchall()
        return tuple(sorted(str(row["role"]) for row in rows if row["role"] in PERMISSIONS))

    def _principal(
        self, connection: sqlite3.Connection, *, user_id: str, workspace_id: str
    ) -> Principal:
        user = connection.execute(
            "SELECT display_name, status FROM users WHERE id=?", (user_id,)
        ).fetchone()
        workspace = connection.execute(
            "SELECT status FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        if (
            user is None
            or workspace is None
            or user["status"] != "active"
            or workspace["status"] != "active"
        ):
            raise AuthenticationError("identity is no longer active")
        roles = self._roles(connection, workspace_id=workspace_id, user_id=user_id)
        if not roles:
            raise AuthenticationError("identity is no longer active")
        return Principal(
            user_id=user_id,
            display_name=str(user["display_name"]),
            workspace_id=workspace_id,
            roles=roles,
        )

    def _verify_password(self, connection: sqlite3.Connection, *, email: str, password: str) -> str:
        """Return the user id for a correct password, or raise a generic error."""
        row = connection.execute(
            "SELECT id, password_salt, password_hash, status FROM users WHERE email=?", (email,)
        ).fetchone()
        if row is None:
            # Burn comparable work so an unknown address is not faster than a wrong
            # password for a real account.
            _digest_password(password, _new_salt())
            raise AuthenticationError(_INVALID_CREDENTIALS)
        digest = _digest_password(password, str(row["password_salt"]))
        if not hmac.compare_digest(digest, str(row["password_hash"])):
            raise AuthenticationError(_INVALID_CREDENTIALS)
        if row["status"] != "active":
            raise AuthenticationError(_INVALID_CREDENTIALS)
        return str(row["id"])

    def _failures(self, connection: sqlite3.Connection, *, email: str) -> int:
        since = _iso(self._now() - self._failure_window)
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM login_failures WHERE email=? AND at>=?", (email, since)
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    @property
    def session_ttl(self) -> timedelta:
        return self._session_ttl

    def login(self, *, email: str, password: str, workspace_id: str) -> LoginResult:
        normalized = _normalize_email(email)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            principal = self._password_principal(
                connection,
                email=normalized,
                password=password,
                workspace_id=workspace_id,
                now=now,
                action="login",
            )
            if isinstance(principal, Principal):
                return self._issue_web_session(connection, principal=principal, now=now)
        # Commit denied attempts before raising; otherwise SQLite rolls back both
        # the failure counter and its audit. The check and accounting share a lock
        # so concurrent requests cannot all pass a stale throttle counter.
        raise principal

    def _password_principal(
        self,
        connection: sqlite3.Connection,
        *,
        email: str,
        password: str,
        workspace_id: str,
        now: datetime,
        action: str,
        permission: str | None = None,
    ) -> Principal | ScopeError:
        """Authenticate inside the caller's write transaction on either channel."""
        if self._failures(connection, email=email) >= self._max_failures:
            self._audit(
                connection,
                at=now,
                action=action,
                outcome="throttled",
                workspace_id=workspace_id,
                detail={"email": email},
            )
            return LoginThrottled()
        try:
            user_id = self._verify_password(connection, email=email, password=password)
            principal = self._principal(connection, user_id=user_id, workspace_id=workspace_id)
            if permission is not None:
                principal.require(permission)
        except ScopeError as exc:
            connection.execute(
                "INSERT INTO login_failures(email, at) VALUES(?,?)", (email, _iso(now))
            )
            self._audit(
                connection,
                at=now,
                action=action,
                outcome="denied",
                workspace_id=workspace_id,
                detail={"email": email},
            )
            return exc
        connection.execute("DELETE FROM login_failures WHERE email=?", (email,))
        return principal

    def _issue_web_session(
        self,
        connection: sqlite3.Connection,
        *,
        principal: Principal,
        now: datetime,
        action: str = "login",
    ) -> LoginResult:
        token = _new_token()
        csrf = secrets.token_urlsafe(24)
        expires_at = now + self._session_ttl
        connection.execute(
            "INSERT INTO web_sessions(token_hash, user_id, workspace_id, csrf, issued_at, "
            "expires_at) VALUES(?,?,?,?,?,?)",
            (
                _token_digest(token),
                principal.user_id,
                principal.workspace_id,
                csrf,
                _iso(now),
                _iso(expires_at),
            ),
        )
        self._audit(
            connection,
            at=now,
            action=action,
            outcome="allowed",
            actor_id=principal.actor_id,
            workspace_id=principal.workspace_id,
        )
        return LoginResult(token=token, csrf=csrf, expires_at=expires_at, principal=principal)

    def logout(self, *, token: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT user_id, workspace_id FROM web_sessions WHERE token_hash=?",
                (_token_digest(token),),
            ).fetchone()
            connection.execute(
                "UPDATE web_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (_iso(self._now()), _token_digest(token)),
            )
            if row is not None:
                self._audit(
                    connection,
                    at=self._now(),
                    action="logout",
                    outcome="allowed",
                    actor_id=f"{_ACTOR_PREFIX}{row['user_id']}",
                    workspace_id=str(row["workspace_id"]),
                )

    def _session_row(self, table: str, token: str) -> sqlite3.Row:
        with self._connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                f"SELECT * FROM {table} WHERE token_hash=?",
                (_token_digest(token),),
            ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or _moment(str(row["expires_at"])) <= self._now()
        ):
            raise AuthenticationError("credential is not valid")
        return row

    def authenticate(self, token: str) -> Principal:
        """Re-read account state on every call; revocation is immediate."""
        row = self._session_row("web_sessions", token)
        with self._connect() as connection:
            return self._principal(
                connection, user_id=str(row["user_id"]), workspace_id=str(row["workspace_id"])
            )

    def current_principal(self, *, user_id: str, workspace_id: str) -> Principal:
        """Revalidate an already bound identity for a trusted runtime callback.

        This is not authentication: callers must obtain the identifiers from a
        previously authenticated run binding, never from an untrusted request.
        No bearer credential is persisted in the execution context.
        """
        with self._connect() as connection:
            return self._principal(connection, user_id=user_id, workspace_id=workspace_id)

    def session_csrf(self, token: str) -> str:
        """Return the live CSRF token so a browser can store it after reload."""
        return str(self._session_row("web_sessions", token)["csrf"])

    def verify_csrf(self, *, token: str, csrf: str) -> bool:
        """Check a CSRF token against the live session row, never a cookie copy."""
        if not csrf:
            return False
        row = self._session_row("web_sessions", token)
        return hmac.compare_digest(str(row["csrf"]), csrf)

    def approve_session(self, *, email: str, password: str, workspace_id: str) -> ApprovalGrant:
        """Issue a short-lived approver credential on a channel of its own."""
        normalized = _normalize_email(email)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            principal = self._password_principal(
                connection,
                email=normalized,
                password=password,
                workspace_id=workspace_id,
                now=now,
                action="approval.session",
                permission="approval:decide",
            )
            if isinstance(principal, Principal):
                token = _new_token()
                expires_at = now + self._approval_ttl
                connection.execute(
                    "INSERT INTO approval_sessions(token_hash, user_id, workspace_id, issued_at, "
                    "expires_at) VALUES(?,?,?,?,?)",
                    (
                        _token_digest(token),
                        principal.user_id,
                        principal.workspace_id,
                        _iso(now),
                        _iso(expires_at),
                    ),
                )
                self._audit(
                    connection,
                    at=now,
                    action="approval.session",
                    outcome="allowed",
                    actor_id=principal.actor_id,
                    workspace_id=principal.workspace_id,
                )
                return ApprovalGrant(token=token, expires_at=expires_at, principal=principal)
        raise principal

    def authenticate_approver(self, token: str) -> Principal:
        """Resolve an approver independently of any owner browser credential."""
        row = self._session_row("approval_sessions", token)
        with self._connect() as connection:
            principal = self._principal(
                connection, user_id=str(row["user_id"]), workspace_id=str(row["workspace_id"])
            )
        principal.require("approval:decide")
        return principal

    # -- consultation session binding --------------------------------------------

    def bind_session(self, *, principal: Principal, session_id: str) -> str:
        """Bind a consultation session id to its owner; one owner per workspace."""
        principal.require("consult")
        _validate_session_id(session_id)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_user_id, closed_at FROM consultation_sessions "
                "WHERE workspace_id=? AND session_id=?",
                (principal.workspace_id, session_id),
            ).fetchone()
            if row is not None:
                if row["owner_user_id"] != principal.user_id or row["closed_at"] is not None:
                    raise ScopeError("session binding is unavailable")
                return principal.user_id
            connection.execute(
                "INSERT INTO consultation_sessions(workspace_id, session_id, owner_user_id, "
                "created_at) VALUES(?,?,?,?)",
                (principal.workspace_id, session_id, principal.user_id, _iso(now)),
            )
            self._audit(
                connection,
                at=now,
                action="session.bind",
                outcome="allowed",
                actor_id=principal.actor_id,
                workspace_id=principal.workspace_id,
                object_id=session_id,
            )
        return principal.user_id

    def session_owner(self, *, principal: Principal, session_id: str) -> str:
        """Return the bound owner or raise the same error for absent and foreign ids."""
        _validate_session_id(session_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_user_id FROM consultation_sessions "
                "WHERE workspace_id=? AND session_id=?",
                (principal.workspace_id, session_id),
            ).fetchone()
        if row is None or str(row["owner_user_id"]) != principal.user_id:
            raise ScopeError("session binding is unavailable")
        return str(row["owner_user_id"])

    # -- object-level authorization ----------------------------------------------

    def require_ticket_read(self, *, principal: Principal, ticket: Mapping[str, object]) -> None:
        """Object-level check: tenant, requester and role, in that order."""
        if str(ticket.get("tenant")) != principal.workspace_id:
            raise ScopeError("ticket is unavailable")
        if str(ticket.get("requester_id") or "") == principal.user_id and principal.has(
            "ticket:read_own"
        ):
            return
        if principal.has("ticket:read_scope"):
            return
        raise ScopeError("ticket is unavailable")

    # -- execution binding -------------------------------------------------------

    def context_for(
        self,
        principal: Principal,
        *,
        application_id: str,
        application_version: str,
        policy_version: str,
        session_id: str,
        resources: Sequence[str] = (),
        correlation_id: str = "request",
        run_id: str | None = None,
    ) -> RunContext:
        """Build the trusted binding from verified identity and server-side scope."""
        return RunContext(
            application_id=application_id,
            application_version=application_version,
            policy_version=policy_version,
            run_id=run_id or f"req-{uuid.uuid4().hex}",
            session_id=session_id,
            actor=principal.actor,
            scope=ResourceScope(
                tenant_id=principal.workspace_id, resources=tuple(dict.fromkeys(resources))
            ),
            correlation_id=correlation_id,
        )

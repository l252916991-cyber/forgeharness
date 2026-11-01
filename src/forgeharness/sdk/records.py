"""Scoped, reviewed content revisions for evidence, knowledge, drafts and lessons.

This is a local business store, separate from execution approvals and run evidence.
Hosts authenticate callers and may replace RoleRecordPolicy with live authorization.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from pydantic import AwareDatetime, Field

from forgeharness.domain.models import FrozenModel, RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform
from forgeharness.state.sqlite import connect_wal


class RecordConflict(ValueError):
    """A revision, request identity or lifecycle precondition changed."""


class RecordUnavailable(ValueError):
    """No current published, unexpired record is available."""


class RecordOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    REVIEW = "review"
    PUBLISH = "publish"
    WITHDRAW = "withdraw"
    DELETE = "delete"


class RecordPolicy(Protocol):
    def authorize(
        self, context: RunContext, operation: RecordOperation, resource_id: str
    ) -> bool: ...


class RoleRecordPolicy:
    """Small opt-in role policy; trusted host contexts must reflect current grants."""

    _roles: ClassVar[dict[RecordOperation, set[str]]] = {
        RecordOperation.READ: {"reader", "editor", "reviewer", "publisher", "admin"},
        RecordOperation.WRITE: {"editor", "admin"},
        RecordOperation.REVIEW: {"reviewer", "admin"},
        RecordOperation.PUBLISH: {"publisher", "admin"},
        RecordOperation.WITHDRAW: {"publisher", "admin"},
        RecordOperation.DELETE: {"admin"},
    }

    def authorize(self, context: RunContext, operation: RecordOperation, resource_id: str) -> bool:
        return resource_id in context.scope.resources and bool(
            self._roles[operation].intersection(context.actor.roles)
        )


class SourceAnchor(FrozenModel):
    """Host-provided provenance, e.g. physical page, API field or video time span.

    This metadata identifies a source; it does not attest its truth or currentness.
    """

    source: str = Field(min_length=1, max_length=1000)
    version: str = Field(min_length=1, max_length=200)
    locator: str = Field(min_length=1, max_length=500)


class RecordDependency(FrozenModel):
    """Exact current publication on which a derived record depends."""

    resource_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class RecordDraft(FrozenModel):
    resource_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=100_000)
    sources: tuple[SourceAnchor, ...] = Field(default=(), max_length=100)
    dependencies: tuple[RecordDependency, ...] = Field(default=(), max_length=100)
    expires_at: AwareDatetime | None = None


class ContentRevision(RecordDraft):
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["draft", "approved", "rejected", "published", "withdrawn"]
    author: str
    reviewer: str | None = None
    created_at: AwareDatetime
    schema_version: Literal[1] = 1


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class RevisionStore:
    """Local SQLite revisions with CAS, exact review and atomic audit events.

    All queries include application and tenant before content is read. Editing
    immediately withdraws the old publication. Historical reads remain explicit.
    """

    def __init__(
        self,
        path: Path,
        *,
        policy: RecordPolicy,
        transform: EvidenceTransform | None = None,
        allow_self_review: bool = False,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise ScopeError("record directory must be owner-private (mode 0700)")
        self.path = path
        self.policy = policy
        self.transform = transform or CredentialMask()
        self.allow_self_review = allow_self_review
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS record_schema (version INTEGER NOT NULL)")
            versions = db.execute("SELECT version FROM record_schema").fetchall()
            if not versions:
                db.execute("INSERT INTO record_schema VALUES(1)")
            elif len(versions) != 1 or versions[0][0] != 1:
                raise RecordConflict("unsupported record schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS record_heads (app TEXT, tenant TEXT, resource TEXT, "
                "version INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(app,tenant,resource))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS record_revisions (app TEXT, tenant TEXT, "
                "resource TEXT, version INTEGER, payload TEXT NOT NULL, "
                "PRIMARY KEY(app,tenant,resource,version))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS record_requests (app TEXT, tenant TEXT, "
                "request_id TEXT, digest TEXT, resource TEXT, version INTEGER, "
                "PRIMARY KEY(app,tenant,request_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS record_events (sequence INTEGER PRIMARY KEY, "
                "app TEXT, tenant TEXT, resource TEXT, payload TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(connect_wal(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _authorize(
        self, context: RunContext, operation: RecordOperation, resource: str
    ) -> tuple[str, str, str]:
        RunContext.model_validate(context.model_dump())
        if (
            self.transform.transform(context.model_dump(mode="json"))
            != context.model_dump(mode="json")
            or resource not in context.scope.resources
            or not self.policy.authorize(context, operation, resource)
        ):
            raise ScopeError("record operation outside current authorization")
        return context.application_id, context.scope.tenant_id, resource

    @staticmethod
    def _head(db: sqlite3.Connection, key: tuple[str, str, str]) -> int:
        row = db.execute(
            "SELECT version, deleted FROM record_heads WHERE app=? AND tenant=? AND resource=?",
            key,
        ).fetchone()
        if row is not None and row["deleted"]:
            raise RecordUnavailable("record was deleted; identity cannot be reused")
        return int(row["version"]) if row else 0

    @staticmethod
    def _revision(
        db: sqlite3.Connection, key: tuple[str, str, str], version: int
    ) -> ContentRevision:
        row = db.execute(
            "SELECT payload FROM record_revisions "
            "WHERE app=? AND tenant=? AND resource=? AND version=?",
            (*key, version),
        ).fetchone()
        if row is None:
            raise RecordUnavailable("record revision unavailable")
        result = ContentRevision.model_validate_json(row[0])
        draft = RecordDraft.model_validate(
            {name: getattr(result, name) for name in RecordDraft.model_fields}
        )
        if (
            result.resource_id != key[2]
            or result.version != version
            or result.digest != _digest(draft.model_dump(mode="json"))
        ):
            raise RecordConflict("record content integrity mismatch")
        return result

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        key: tuple[str, str, str],
        context: RunContext,
        operation: str,
        revision: ContentRevision | None = None,
    ) -> None:
        event = {
            "operation": operation,
            "actor": context.actor.id,
            "at": datetime.now(UTC).isoformat(),
            "version": revision.version if revision else None,
            "digest": revision.digest if revision else None,
        }
        db.execute(
            "INSERT INTO record_events(app,tenant,resource,payload) VALUES(?,?,?,?)",
            (*key, json.dumps(event)),
        )

    def save(
        self,
        context: RunContext,
        draft: RecordDraft,
        *,
        expected_version: int,
        request_id: str,
    ) -> ContentRevision:
        """Create an immutable draft; stable host request ID makes retries idempotent."""
        draft = RecordDraft.model_validate(draft.model_dump())
        key = self._authorize(context, RecordOperation.WRITE, draft.resource_id)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        if (
            not request_id
            or len(request_id) > 200
            or self.transform.transform(request_id) != request_id
        ):
            raise ValueError("request_id must be a bounded, credential-free host identity")
        clean = RecordDraft.model_validate(self.transform.transform(draft.model_dump(mode="json")))
        if clean.resource_id != draft.resource_id:
            raise ScopeError("record identity must not contain credentials")
        payload = clean.model_dump(mode="json")
        request_digest = _digest([payload, expected_version, context.actor.id])
        with self._connect() as db:
            head = self._head(db, key)
            old = db.execute(
                "SELECT digest,resource,version FROM record_requests "
                "WHERE app=? AND tenant=? AND request_id=?",
                (*key[:2], request_id),
            ).fetchone()
            if old:
                if old["digest"] != request_digest or old["resource"] != key[2]:
                    raise RecordConflict("request identity reused with different payload")
                return self._revision(db, key, int(old["version"]))
            if head != expected_version:
                raise RecordConflict("record changed since it was read")
            self._dependencies(db, context, clean, frozenset({clean.resource_id}), {})
            revision = ContentRevision(
                **payload,
                version=head + 1,
                digest=_digest(payload),
                state="draft",
                author=context.actor.id,
                created_at=datetime.now(UTC),
            )
            if head:
                previous = self._revision(db, key, head)
                self._put(db, key, previous.model_copy(update={"state": "withdrawn"}))
            self._put(db, key, revision)
            db.execute(
                "INSERT INTO record_heads VALUES(?,?,?,?,0) ON CONFLICT(app,tenant,resource) "
                "DO UPDATE SET version=excluded.version",
                (*key, revision.version),
            )
            db.execute(
                "INSERT INTO record_requests VALUES(?,?,?,?,?,?)",
                (*key[:2], request_id, request_digest, key[2], revision.version),
            )
            self._event(db, key, context, "draft.created", revision)
            return revision

    @staticmethod
    def _put(db: sqlite3.Connection, key: tuple[str, str, str], value: ContentRevision) -> None:
        db.execute(
            "INSERT INTO record_revisions VALUES(?,?,?,?,?) "
            "ON CONFLICT(app,tenant,resource,version) DO UPDATE SET payload=excluded.payload",
            (*key, value.version, value.model_dump_json()),
        )

    def read(
        self, context: RunContext, resource_id: str, *, version: int | None = None
    ) -> ContentRevision:
        key = self._authorize(context, RecordOperation.READ, resource_id)
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError("version must be a positive integer")
        with self._connect() as db:
            head = self._head(db, key)
            return self._revision(db, key, head if version is None else version)

    def published(self, context: RunContext, resource_id: str) -> ContentRevision:
        key = self._authorize(context, RecordOperation.READ, resource_id)
        with self._connect() as db:
            return self._published(db, context, key, frozenset(), {})

    def _published(
        self,
        db: sqlite3.Connection,
        context: RunContext,
        key: tuple[str, str, str],
        seen: frozenset[str],
        cache: dict[str, ContentRevision],
    ) -> ContentRevision:
        if key[2] in seen or len(seen) >= 32:
            raise RecordConflict("cyclic or excessively deep record dependencies")
        if key[2] in cache:
            return cache[key[2]]
        if len(cache) >= 256:
            raise RecordConflict("record dependency validation exceeds 256 sources")
        revision = self._revision(db, key, self._head(db, key))
        if revision.state != "published" or (
            revision.expires_at is not None and revision.expires_at <= datetime.now(UTC)
        ):
            raise RecordUnavailable("record is unpublished, withdrawn or expired")
        cache[key[2]] = revision
        self._dependencies(db, context, revision, seen | {key[2]}, cache)
        return revision

    def _dependencies(
        self,
        db: sqlite3.Connection,
        context: RunContext,
        value: RecordDraft,
        seen: frozenset[str],
        cache: dict[str, ContentRevision],
    ) -> None:
        for dependency in value.dependencies:
            key = self._authorize(context, RecordOperation.READ, dependency.resource_id)
            current = self._published(db, context, key, seen, cache)
            if current.version != dependency.version or current.digest != dependency.digest:
                raise RecordUnavailable("derived record references a superseded source")

    def review(
        self, context: RunContext, resource_id: str, *, expected_version: int, approve: bool
    ) -> ContentRevision:
        if type(approve) is not bool:
            raise ValueError("approve must be a boolean")
        return self._transition(
            context,
            resource_id,
            expected_version,
            RecordOperation.REVIEW,
            "approved" if approve else "rejected",
        )

    def publish(
        self, context: RunContext, resource_id: str, *, expected_version: int
    ) -> ContentRevision:
        return self._transition(
            context, resource_id, expected_version, RecordOperation.PUBLISH, "published"
        )

    def withdraw(
        self, context: RunContext, resource_id: str, *, expected_version: int
    ) -> ContentRevision:
        return self._transition(
            context, resource_id, expected_version, RecordOperation.WITHDRAW, "withdrawn"
        )

    def _transition(
        self,
        context: RunContext,
        resource_id: str,
        version: int,
        operation: RecordOperation,
        target: Literal["approved", "rejected", "published", "withdrawn"],
    ) -> ContentRevision:
        key = self._authorize(context, operation, resource_id)
        with self._connect() as db:
            if type(version) is not int or version != self._head(db, key):
                raise RecordConflict("review or publication refers to a stale revision")
            revision = self._revision(db, key, version)
            if operation == RecordOperation.REVIEW:
                if revision.state != "draft":
                    raise RecordConflict("only the current draft can be reviewed")
                if not self.allow_self_review and revision.author == context.actor.id:
                    raise ScopeError("an independent reviewer is required")
            if operation == RecordOperation.PUBLISH and revision.state != "approved":
                raise RecordConflict("publication requires exact-revision business review")
            if target in {"approved", "published"}:
                self._dependencies(db, context, revision, frozenset({resource_id}), {})
            if (
                target == "published"
                and revision.expires_at
                and revision.expires_at <= datetime.now(UTC)
            ):
                raise RecordUnavailable("expired revision cannot be published")
            changes: dict[str, Any] = {"state": target}
            if operation == RecordOperation.REVIEW:
                changes["reviewer"] = context.actor.id
            revision = revision.model_copy(update=changes)
            self._put(db, key, revision)
            self._event(db, key, context, target, revision)
            return revision

    def audit(self, context: RunContext, resource_id: str) -> tuple[dict[str, Any], ...]:
        key = self._authorize(context, RecordOperation.READ, resource_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM record_events WHERE app=? AND tenant=? AND resource=? "
                "ORDER BY sequence",
                key,
            ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def delete(self, context: RunContext, resource_id: str) -> None:
        """Purge local content and audit copies; retain an identity tombstone only."""
        key = self._authorize(context, RecordOperation.DELETE, resource_id)
        with self._connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            for table in ("record_revisions", "record_requests", "record_events"):
                db.execute(f"DELETE FROM {table} WHERE app=? AND tenant=? AND resource=?", key)
            db.execute(
                "INSERT INTO record_heads VALUES(?,?,?,0,1) ON CONFLICT(app,tenant,resource) "
                "DO UPDATE SET version=0, deleted=1",
                key,
            )

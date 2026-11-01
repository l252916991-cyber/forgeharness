"""Staged, validated and versioned material imports (ADR 015).

The chain is staging → validation → parse → draft revision → human review →
publication → indexing. Staging never grants trust: a staged artifact cannot be read
as material by the runtime, and publication happens only through ``RevisionStore``.
Parsing and validation are host supplied, so the base SDK keeps no media, network or
model dependency, and the durable task store stays domain-agnostic: everything an
import needs is persisted here, keyed by the task's business key.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from forgeharness.domain.models import FrozenModel, RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform
from forgeharness.sdk.records import ContentRevision, RecordDraft, RevisionStore, SourceAnchor
from forgeharness.sdk.tasks import TaskHandler, TaskOutcome, TaskRecord, TaskState, TaskStore
from forgeharness.state.sqlite import connect_wal
from forgeharness.tools.base import ToolEffectClass

MAX_STAGED_BYTES = 10 * 1024 * 1024
MAX_STAGED_FILES = 1_000
MAX_PARSED_CHARS = 100_000
MAX_FILENAME_CHARS = 200

# Draft creation is idempotent on the business key: a repeated attempt returns the
# existing revision instead of creating a second one, which is what makes task
# retries safe. Claiming a weaker class here would force needless reconciliation.
_DRAFT_EFFECT = ToolEffectClass.IDEMPOTENT

# A task that is not terminal still needs its staged bytes to be able to run at all.
_NON_TERMINAL_STATES = (TaskState.QUEUED, TaskState.RUNNING, TaskState.NEEDS_RECONCILIATION)


class ImportConflict(ValueError):
    """A staged import conflicts with durable import state."""


class ImportUnavailable(ScopeError):
    """Staged content, its lease or its binding is not available to this caller."""


class ImportOperation(StrEnum):
    STAGE = "stage"
    READ = "read"
    VALIDATE = "validate"
    DRAFT = "draft"
    SWEEP = "sweep"


class StagedMaterial(FrozenModel):
    """A content-addressed staging record. Metadata only; content is not trusted."""

    id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=MAX_FILENAME_CHARS)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=1, le=MAX_STAGED_BYTES)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    application_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    staged_by: str = Field(min_length=1, max_length=200)
    created_at: datetime
    expires_at: datetime
    schema_version: Literal[1] = 1


class ParsedSpan(FrozenModel):
    """A locatable region of the parsed text, for page or section level anchors."""

    locator: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class ParsedDocument(FrozenModel):
    content: str = Field(min_length=1, max_length=MAX_PARSED_CHARS)
    spans: tuple[ParsedSpan, ...] = ()


class ValidationOutcome(FrozenModel):
    """The recorded verdict of the host validator for one staged artifact."""

    staged_id: str
    accepted: bool
    reason: str = Field(min_length=1, max_length=500)
    validated_at: datetime
    schema_version: Literal[1] = 1


class ImportRequest(FrozenModel):
    """What an import task must do, persisted next to the task rather than inside it."""

    staged_id: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)
    schema_version: Literal[1] = 1


class RetentionPolicy(FrozenModel):
    """Explicit lifetimes. Nothing is removed implicitly by age alone."""

    staging_seconds: int = Field(default=86_400, ge=60, le=30 * 86_400)


class MaterialParser(Protocol):
    """Trusted host adapter. Deterministic, and it must not import repository code."""

    def parse(self, *, filename: str, data: bytes, content_type: str) -> ParsedDocument: ...


class MaterialValidator(Protocol):
    """Trusted host adapter. Decides whether staged content may become a draft."""

    def validate(
        self, context: RunContext, staged: StagedMaterial, parsed: ParsedDocument
    ) -> tuple[bool, str]: ...


class _ImportHandler:
    """The ``import`` task handler: parse, validate and draft, recording progress."""

    kind = "import"

    def __init__(self, service: ImportService) -> None:
        self.service = service

    async def run(self, context: RunContext, task: TaskRecord) -> TaskOutcome:
        request = self.service.import_request(context, task)
        if self.service.tasks is not None:
            self.service.tasks.progress(context, task.id, done=1)
        # draft parses and validates the same in-memory snapshot before saving.
        # Pre-validating here would both duplicate work and invite cached-verdict reuse.
        revision = self.service.draft(
            context,
            request=request,
            request_id=task.request_id,
        )
        return TaskOutcome(
            # Task metadata outlives a revision; it must not retain a second copy
            # of content that RevisionStore.delete cannot erase.
            result=payload_json(
                {
                    "resource_id": revision.resource_id,
                    "version": revision.version,
                    "digest": revision.digest,
                    "state": revision.state,
                }
            ),
            progress_done=3,
            progress_total=3,
        )


class ImportService:
    """Local staging plus import orchestration over ``RevisionStore``.

    ``authorize`` is mandatory and must query current host permissions, exactly as in
    ``SessionService``: a staged artifact records who staged it, but every read,
    validation, draft and sweep is authorized again for the caller at hand.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        records: RevisionStore,
        parser: MaterialParser,
        validator: MaterialValidator,
        authorize: Callable[[RunContext, ImportOperation, StagedMaterial | None], bool],
        tasks: TaskStore | None = None,
        retention: RetentionPolicy | None = None,
        transform: EvidenceTransform | None = None,
        max_bytes: int = MAX_STAGED_BYTES,
    ) -> None:
        if not callable(authorize):
            raise ValueError("a live host authorizer is required")
        if not 1 <= max_bytes <= MAX_STAGED_BYTES:
            raise ValueError("invalid staging size bound")
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.state_dir.stat().st_mode) & 0o077:
            raise ScopeError("import directory must be owner-private (mode 0700)")
        self.staging_dir = self.state_dir / "staging"
        self.staging_dir.mkdir(exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.staging_dir.stat().st_mode) & 0o077:
            raise ScopeError("staging directory must be owner-private (mode 0700)")
        self.records = records
        self.parser = parser
        self.validator = validator
        self.authorize = authorize
        self.tasks = tasks
        self.retention = retention or RetentionPolicy()
        self.transform = transform or CredentialMask()
        self.max_bytes = max_bytes
        self.database = self.state_dir / "imports.sqlite"
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS import_schema (version INTEGER NOT NULL)")
            versions = db.execute("SELECT version FROM import_schema").fetchall()
            if not versions:
                db.execute("INSERT INTO import_schema VALUES(1)")
            elif versions != [(1,)]:
                raise ImportConflict("unsupported import schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS staged_materials (id TEXT PRIMARY KEY, payload TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS staged_validations (staged_id TEXT PRIMARY KEY, "
                "payload TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS import_requests (business_key TEXT PRIMARY KEY, "
                "payload TEXT)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(connect_wal(self.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _authorize(
        self,
        context: RunContext,
        operation: ImportOperation,
        staged: StagedMaterial | None,
        *,
        allow_expired: bool = False,
    ) -> None:
        RunContext.model_validate(context.model_dump())
        if self.transform.transform(context.model_dump(mode="json")) != context.model_dump(
            mode="json"
        ):
            raise ImportUnavailable("credentials are not accepted in a trusted identity")
        if staged is not None and (
            staged.application_id != context.application_id
            or staged.tenant_id != context.scope.tenant_id
            or (
                staged.staged_by != context.actor.id
                and f"staged:{staged.id}" not in context.scope.resources
            )
        ):
            raise ImportUnavailable("staged material is outside the current binding")
        if staged is not None and not allow_expired and staged.expires_at <= datetime.now(UTC):
            raise ImportUnavailable("staged material lease expired")
        if self.authorize(context, operation, staged) is not True:
            raise ImportUnavailable("current import permission denied")

    def _staged(self, context: RunContext, staged_id: str) -> StagedMaterial:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM staged_materials WHERE id=?", (staged_id,)
            ).fetchone()
        if row is None:
            raise ImportUnavailable("staged material unavailable")
        return StagedMaterial.model_validate_json(row[0])

    def staged(self, context: RunContext, staged_id: str) -> StagedMaterial:
        staged = self._staged(context, staged_id)
        self._authorize(context, ImportOperation.READ, staged)
        return staged

    def _path(self, staged: StagedMaterial) -> Path:
        return self.staging_dir / staged.id

    def stage(
        self,
        context: RunContext,
        *,
        filename: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> StagedMaterial:
        """Write bounded bytes to the staging area. Content addressing makes it idempotent."""
        self._authorize(context, ImportOperation.STAGE, None)
        if not isinstance(filename, str) or not filename or len(filename) > MAX_FILENAME_CHARS:
            raise ValueError("filename must be a bounded single name")
        if Path(filename).name != filename or filename in {".", ".."}:
            raise ValueError("filename must be a single relative component")
        if not isinstance(data, bytes) or not data:
            raise ValueError("staged content must be non-empty bytes")
        if len(data) > self.max_bytes:
            raise ValueError("staged content exceeds the configured bound")
        if not content_type or len(content_type) > 100:
            raise ValueError("content type must be bounded")
        digest = hashlib.sha256(data).hexdigest()
        staged_id = hashlib.sha256(
            payload_json(
                [context.application_id, context.scope.tenant_id, context.actor.id, digest]
            ).encode()
        ).hexdigest()
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM staged_materials WHERE id=?", (staged_id,)
            ).fetchone()
            if row is not None:
                existing = StagedMaterial.model_validate_json(row[0])
                if existing.digest != digest:
                    raise ImportConflict("staged identity reused with different content")
                self._authorize(context, ImportOperation.READ, existing)
                return existing
            count = db.execute("SELECT COUNT(*) FROM staged_materials").fetchone()[0]
            if count >= MAX_STAGED_FILES:
                raise ImportConflict("staging area is full; sweep expired leases first")
            # One clock reading, so the recorded lease is exactly the configured one.
            staged_at = datetime.now(UTC)
            staged = StagedMaterial(
                id=staged_id,
                filename=filename,
                content_type=content_type,
                size_bytes=len(data),
                digest=digest,
                application_id=context.application_id,
                tenant_id=context.scope.tenant_id,
                staged_by=context.actor.id,
                created_at=staged_at,
                expires_at=staged_at + timedelta(seconds=self.retention.staging_seconds),
            )
            self._write_blob(staged, data)
            db.execute(
                "INSERT INTO staged_materials VALUES(?,?)",
                (staged.id, staged.model_dump_json()),
            )
        return staged

    def _write_blob(self, staged: StagedMaterial, data: bytes) -> None:
        """Temp file then atomic replace; a crash leaves an orphan that the sweep removes."""
        destination = self._path(staged)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != staged.digest:
                raise ImportConflict("staged blob does not match its recorded digest")
            return
        handle = tempfile.NamedTemporaryFile(dir=self.staging_dir, prefix="stage-", delete=False)
        try:
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, destination)
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def _read_blob(self, staged: StagedMaterial) -> bytes:
        try:
            data = self._path(staged).read_bytes()
        except OSError as exc:
            raise ImportUnavailable("staged content is unavailable") from exc
        if hashlib.sha256(data).hexdigest() != staged.digest:
            raise ImportUnavailable("staged content failed its integrity check")
        return data

    def parse(self, context: RunContext, staged_id: str) -> ParsedDocument:
        """Parse staged content and apply the evidence transform before it is stored."""
        staged = self.staged(context, staged_id)
        parsed = self.parser.parse(
            filename=staged.filename,
            data=self._read_blob(staged),
            content_type=staged.content_type,
        )
        clean = self.transform.transform(parsed.content)
        if not isinstance(clean, str) or not clean:
            raise ImportConflict("parsed content is empty after the evidence transform")
        if len(clean) > MAX_PARSED_CHARS:
            # The revision content bound is deliberate: a document that does not fit is
            # a host decision to split, never a silent truncation.
            raise ImportConflict(
                "parsed content exceeds the revision bound; split the document before import"
            )
        for span in parsed.spans:
            if span.end > len(parsed.content):
                raise ValueError("parsed span lies outside the parsed content")
        return ParsedDocument(content=clean, spans=parsed.spans)

    def validate(self, context: RunContext, staged_id: str) -> ValidationOutcome:
        """Parse and validate once, and remember the verdict for this staged artifact."""
        staged = self.staged(context, staged_id)
        self._authorize(context, ImportOperation.VALIDATE, staged)
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM staged_validations WHERE staged_id=?", (staged.id,)
            ).fetchone()
        if row is not None:
            return ValidationOutcome.model_validate_json(row[0])
        parsed = self.parse(context, staged_id)
        return self._validate_parsed(context, staged, parsed)

    def _validate_parsed(
        self, context: RunContext, staged: StagedMaterial, parsed: ParsedDocument
    ) -> ValidationOutcome:
        """Validate the exact snapshot the caller will save, under current permission.

        The standalone validate endpoint may display a previous verdict, but a
        draft always invokes this method on its own single parse. Replacing a
        parser or validator between restarts cannot transfer an earlier acceptance
        to different content.
        """
        self._authorize(context, ImportOperation.VALIDATE, staged)
        decision = self.validator.validate(context, staged, parsed)
        if not isinstance(decision, tuple) or len(decision) != 2:
            raise ValueError("material validator must return a decision and a reason")
        accepted, reason = decision
        if not isinstance(accepted, bool) or not isinstance(reason, str) or not reason:
            raise ValueError("material validator must return a decision and a reason")
        outcome = ValidationOutcome(
            staged_id=staged.id,
            accepted=accepted,
            reason=str(self.transform.transform(reason))[:500],
            validated_at=datetime.now(UTC),
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO staged_validations VALUES(?,?) ON CONFLICT(staged_id) "
                "DO UPDATE SET payload=excluded.payload",
                (staged.id, outcome.model_dump_json()),
            )
        return outcome

    def draft(
        self,
        context: RunContext,
        *,
        request: ImportRequest,
        request_id: str,
    ) -> ContentRevision:
        """Create the draft revision for a validated staged artifact.

        The business key (``request_id``) is the caller's stable import identity: a
        retry with the same key returns the existing revision instead of creating a
        second one.
        """
        staged = self.staged(context, request.staged_id)
        self._authorize(context, ImportOperation.DRAFT, staged)
        parsed = self.parse(context, request.staged_id)
        outcome = self._validate_parsed(context, staged, parsed)
        if not outcome.accepted:
            raise ImportConflict(f"staged material was rejected: {outcome.reason}")
        sources = tuple(
            SourceAnchor(source=staged.id, version=staged.digest, locator=span.locator)
            for span in parsed.spans
        )
        return self.records.save(
            context,
            RecordDraft(
                resource_id=request.resource_id,
                kind=request.kind,
                content=parsed.content,
                sources=sources,
            ),
            expected_version=request.expected_version,
            request_id=request_id,
        )

    def start_import(
        self,
        context: RunContext,
        *,
        staged_id: str,
        resource_id: str,
        kind: str,
        request_id: str,
        expected_version: int = 0,
    ) -> TaskRecord:
        """Register a durable import task; the handler performs parse/validate/draft."""
        if self.tasks is None:
            raise ImportConflict("no durable task store is configured")
        staged = self.staged(context, staged_id)
        self._authorize(context, ImportOperation.DRAFT, staged)
        request = ImportRequest(
            staged_id=staged_id,
            resource_id=resource_id,
            kind=kind,
            expected_version=expected_version,
        )
        business_key = hashlib.sha256(
            payload_json([staged.id, resource_id, expected_version]).encode()
        ).hexdigest()
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload FROM import_requests WHERE business_key=?", (business_key,)
            ).fetchone()
            if existing is not None and ImportRequest.model_validate_json(existing[0]) != request:
                raise ImportConflict("import business key reused with a different request")
            db.execute(
                "INSERT INTO import_requests VALUES(?,?) ON CONFLICT(business_key) DO NOTHING",
                (business_key, request.model_dump_json()),
            )
            return self.tasks.create(
                context,
                kind=_ImportHandler.kind,
                request_id=request_id,
                business_key=business_key,
                effect_class=_DRAFT_EFFECT,
                progress_total=3,
            )

    def import_request(self, context: RunContext, task: TaskRecord) -> ImportRequest:
        """Read the persisted request of an import task, re-authorized for the caller."""
        if task.kind != _ImportHandler.kind:
            raise ImportConflict("task is not an import task")
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM import_requests WHERE business_key=?", (task.business_key,)
            ).fetchone()
        if row is None:
            raise ImportUnavailable("import request unavailable")
        request = ImportRequest.model_validate_json(row[0])
        staged = self.staged(context, request.staged_id)
        self._authorize(context, ImportOperation.DRAFT, staged)
        return request

    def handlers(self) -> tuple[TaskHandler, ...]:
        """Task handlers this service can run: the import chain."""
        return (_ImportHandler(self),)

    def _protected(self, db: sqlite3.Connection, staged: StagedMaterial) -> bool:
        """Retention checks every linked task, including work hidden from this caller."""
        for key, payload in db.execute("SELECT business_key,payload FROM import_requests"):
            if ImportRequest.model_validate_json(payload).staged_id == staged.id:
                if self.tasks is None:
                    raise ImportConflict(
                        "durable task store required to confirm linked import retention"
                    )
                if self.tasks.has_active_business_key(staged.application_id, staged.tenant_id, key):
                    return True
        return False

    def _remove(self, db: sqlite3.Connection, staged: StagedMaterial) -> None:
        db.execute("DELETE FROM staged_materials WHERE id=?", (staged.id,))
        db.execute("DELETE FROM staged_validations WHERE staged_id=?", (staged.id,))
        self._path(staged).unlink(missing_ok=True)

    def sweep(self, context: RunContext, *, now: datetime | None = None) -> tuple[str, ...]:
        """Remove this caller's expired, authorized leases with no active tasks.

        Blob names are scope-bound identities, never shared across tenants. Global
        orphan cleanup is a host maintenance operation, not a tenant request.
        """
        self._authorize(context, ImportOperation.SWEEP, None)
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError("sweep time must be timezone aware")
        removed: list[str] = []
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM staged_materials").fetchall()
            for row in rows:
                staged = StagedMaterial.model_validate_json(row[0])
                try:
                    self._authorize(context, ImportOperation.SWEEP, staged, allow_expired=True)
                except ImportUnavailable:
                    continue
                if staged.expires_at > moment or self._protected(db, staged):
                    continue
                self._remove(db, staged)
                removed.append(staged.id)
        return tuple(removed)

    def delete_staged(self, context: RunContext, staged_id: str) -> None:
        staged = self._staged(context, staged_id)
        self._authorize(context, ImportOperation.SWEEP, staged, allow_expired=True)
        with self._connect() as db:
            if self._protected(db, staged):
                raise ImportConflict("staged material is still referenced by an active task")
            self._remove(db, staged)


def payload_json(value: object) -> str:
    """Canonical JSON for stored payloads, so equality checks are stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "MAX_PARSED_CHARS",
    "MAX_STAGED_BYTES",
    "ImportConflict",
    "ImportOperation",
    "ImportRequest",
    "ImportService",
    "ImportUnavailable",
    "MaterialParser",
    "MaterialValidator",
    "ParsedDocument",
    "ParsedSpan",
    "RetentionPolicy",
    "StagedMaterial",
    "ValidationOutcome",
    "payload_json",
]

"""Durable local task records, and the single-writer runner that drives them.

Phase S2 (ADR 015) needs import, parse and index work to survive a restart: the
record — not a worker's memory and not conversation text — is the authority on what
was requested, how far it got, and whether its outcome is known. Three rules shape
this module:

* A task identity is derived from ``(application, tenant, request_id)`` and never
  regenerated, so a retry or a restart cannot mint a second identity and therefore
  cannot create a second revision, index row or ticket for one request. The stable
  ``business_key`` is what the host passes to its own idempotent write.
* Retry is decided by ``forgeharness.runtime.retry.decide_retry`` from the effect
  class, the error code and the attempt budget. This module maps that decision onto
  task states and never re-implements the rule. A non-idempotent step whose outcome
  is unknown becomes ``needs_reconciliation``; it is never retried and never called
  "cancelled and done".
* ``flock`` on a per-task lock file is the single-writer guard *across* calls: a
  claim is held for the whole time a handler runs, which is what makes "only the
  claimer may report progress or complete" true for a second process too.

``path`` is the owner-private state directory (mode 0700), like
``sdk.sessions.SessionService``: the store creates ``tasks.sqlite`` and one
``<task id>.lock`` file per claimed task inside it. Everything is local,
single-machine POSIX/SQLite, matching ADR 015 — no distributed lease, no outbox and
no cross-database transaction.

Cancellation is a request, not a rollback. Cancelling a queued task settles it with
no handler execution; cancelling a running task only sets ``cancel_requested`` and
the runner stops at its next safe point. A record settled as cancelled carries
``CANCEL_NOTE`` in ``error``, so a settled cancellation can never be read as "the
executed writes were undone".

The host authorizes every operation through ``authorize`` with the current
``RunContext``; this module is not an identity system, and a context that does not
match the record's application, tenant and actor is refused.
"""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field

from forgeharness.domain.models import FrozenModel, RunContext
from forgeharness.runtime.retry import RetryDecision, decide_retry
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.privacy import CredentialMask
from forgeharness.state.sqlite import connect_wal
from forgeharness.tools.base import ToolEffectClass, ToolErrorCode

TASK_SCHEMA_VERSION = 1

# Every host-supplied string is bounded at the boundary: an unbounded identity or
# payload would make the record's own size a denial-of-service vector, and the
# progress/attempt fields are counted, never open-ended.
MAX_KIND_CHARS = 64
MAX_REQUEST_ID_CHARS = 200
MAX_BUSINESS_KEY_CHARS = 200
MAX_RESULT_CHARS = 100_000
MAX_ERROR_CHARS = 2_000
MAX_LIST_LIMIT = 200
MAX_DRAIN_LIMIT = 100
MAX_ATTEMPTS = 10
MAX_PROGRESS = 1_000_000_000

# A settled cancellation must stay legible as "stopped, not undone" for an operator
# reading the record later; `error` is the only durable free-text field.
CANCEL_NOTE = "cancelled by request; cancellation stops remaining work and is not a rollback"


class TaskState(StrEnum):
    """Lifecycle of one durable task record.

    ``NEEDS_RECONCILIATION`` is deliberately not terminal: the host still has to
    check durable artifacts before the work can be called finished or failed, and
    the framework never guesses which.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class TaskOperation(StrEnum):
    """The host permission checked for each public operation."""

    CREATE = "create"
    READ = "read"
    RUN = "run"
    CANCEL = "cancel"
    DELETE = "delete"


class TaskConflict(ValueError):
    """A request conflicts with the current durable task state."""


class TaskUnavailable(ScopeError):
    """A task is missing, bound to another caller, or not currently permitted."""


_TERMINAL = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})
# States a host may confirm through `reconcile`. RUNNING is refused: only `claim`
# may start a unit of work.
_RECONCILABLE = frozenset(
    {
        TaskState.QUEUED,
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.NEEDS_RECONCILIATION,
    }
)
_RECOVERABLE = (TaskState.RUNNING, TaskState.NEEDS_RECONCILIATION)


class TaskRecord(FrozenModel):
    """One durable unit of host work, addressed by a deterministic identity.

    ``attempts`` counts physical attempts that have finished: it is incremented when
    an attempt settles through ``complete``/``fail`` (or is confirmed by
    ``reconcile``), never by ``claim``, which only starts one.
    """

    id: str
    kind: str
    request_id: str
    business_key: str
    state: TaskState
    effect_class: ToolEffectClass
    progress_done: int = Field(default=0, ge=0)
    progress_total: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    error: str | None = None
    error_code: ToolErrorCode | None = None
    result: str | None = None
    cancel_requested: bool = False
    application_id: str
    tenant_id: str
    actor_id: str
    resources: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime
    schema_version: Literal[1] = 1

    @property
    def terminal(self) -> bool:
        """True once the record can no longer change state."""
        return self.state in _TERMINAL


class TaskOutcome(FrozenModel):
    """What a handler reports after one successful unit of work."""

    result: str | None = None
    progress_done: int | None = None
    progress_total: int | None = None


class TaskHandler(Protocol):
    """Host work addressed by a task's ``kind``.

    ``run`` must be safe to re-run for the same ``business_key`` (that is what makes
    an ``idempotent`` record retryable) or the host must declare
    ``effect_class=non_idempotent``, so a failed attempt is reconciled instead of
    replayed. One ``run`` call is one unit of work: a handler that observes a cancel
    request mid-flight must stop itself and raise, because the runner cannot preempt
    a coroutine.
    """

    kind: str

    async def run(self, context: RunContext, task: TaskRecord) -> TaskOutcome: ...


Authorize = Callable[[RunContext, TaskOperation, TaskRecord | None], bool]


def _digest(value: Any) -> str:
    """Hash a canonical identity so the same request always maps to the same id."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _as_state(value: object) -> TaskState:
    """Accept a state or its serialized value; refuse anything else."""
    if isinstance(value, TaskState):
        return value
    if isinstance(value, str):
        try:
            return TaskState(value)
        except ValueError as exc:
            raise ValueError(f"state must be a TaskState, not {value!r}") from exc
    raise ValueError("state must be a TaskState")


def _as_effect_class(value: object) -> ToolEffectClass:
    """Accept an effect class or its serialized value; refuse anything else."""
    if isinstance(value, ToolEffectClass):
        return value
    if isinstance(value, str):
        try:
            return ToolEffectClass(value)
        except ValueError as exc:
            raise ValueError(f"effect_class must be a ToolEffectClass, not {value!r}") from exc
    raise ValueError("effect_class must be a ToolEffectClass")


def _as_error_code(value: object) -> ToolErrorCode:
    """Accept a tool error code or its serialized value; refuse anything else."""
    if isinstance(value, ToolErrorCode):
        return value
    if isinstance(value, str):
        try:
            return ToolErrorCode(value)
        except ValueError as exc:
            raise ValueError(f"error_code must be a ToolErrorCode, not {value!r}") from exc
    raise ValueError("error_code must be a ToolErrorCode")


def _diagnostic(exc: BaseException) -> str:
    """Bound a host exception into a recorded diagnostic (masked by the store)."""
    return f"{type(exc).__name__}: {exc}".strip()[:MAX_ERROR_CHARS]


class TaskStore:
    """Single-machine durable task records with host authorization.

    ``authorize`` is mandatory and must query current host permissions; an
    always-true callback is suitable only for isolated examples. ``path`` is the
    owner-private state directory, not the database file: the directory has to hold
    the per-task lock files as well, and its mode is what makes those locks
    meaningful.
    """

    def __init__(
        self,
        path: Path,
        *,
        authorize: Authorize,
        max_attempts: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(authorize):
            raise ValueError("a live host authorizer is required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS}")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self.authorize = authorize
        self.max_attempts = max_attempts
        self.clock = clock
        self.transform = CredentialMask()
        self.path = path.resolve()
        if self.path.exists() and not self.path.is_dir():
            # `path` is a directory by contract; refusing a file path here is much
            # clearer than creating a directory named like a database file.
            raise ValueError("task state path must be a directory")
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
            raise TaskUnavailable("task directory must be owner-private (mode 0700)")
        self.database = self.path / "tasks.sqlite"
        # Task id -> open lock file held for as long as this store owns the claim.
        self._locks: dict[str, TextIOWrapper] = {}
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS task_schema (version INTEGER NOT NULL)")
            versions = db.execute("SELECT version FROM task_schema").fetchall()
            if not versions:
                db.execute("INSERT INTO task_schema VALUES(1)")
            elif versions != [(TASK_SCHEMA_VERSION,)]:
                raise TaskConflict("unsupported task schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS task_tombstones (id TEXT PRIMARY KEY, "
                "application_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                "business_key TEXT NOT NULL, "
                "UNIQUE(application_id,tenant_id,business_key))"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE (application_id, tenant_id, request_id),
                    UNIQUE (application_id, tenant_id, business_key)
                )
                """
            )

    # -- plumbing ---------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(connect_wal(self.database)) as db, db:
            # One writer at a time: every transition reads then writes a record, and
            # the state guard is only meaningful inside a serialized transaction.
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _now(self) -> datetime:
        value = datetime.now(UTC) if self.clock is None else self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _identity(self, value: object, *, name: str, limit: int) -> str:
        """Return a bounded, credential-free identity component."""
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
        masked: str = self.transform.transform(value)
        if masked != value:
            raise ValueError(f"{name} must not contain credentials")
        return value

    def _payload(self, value: object, *, name: str, limit: int) -> str:
        """Return a bounded payload, masked exactly like stored evidence."""
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
        clean: str = self.transform.transform(value)
        return clean

    def _result(self, value: object) -> str | None:
        """Return an optional bounded result payload, masked before storage."""
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > MAX_RESULT_CHARS:
            raise ValueError(f"result must be a string of at most {MAX_RESULT_CHARS} characters")
        clean: str = self.transform.transform(value)
        return clean

    def _key(self, context: RunContext) -> None:
        """Validate the caller binding before any record is read or written."""
        RunContext.model_validate(context.model_dump())
        raw = context.model_dump(mode="json")
        if self.transform.transform(raw) != raw:
            raise TaskUnavailable("invalid task binding or credentials in identity")

    def _row(self, db: sqlite3.Connection, task_id: str) -> TaskRecord:
        row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskUnavailable("task unavailable")
        return TaskRecord.model_validate_json(row[0])

    def _write(self, db: sqlite3.Connection, record: TaskRecord) -> None:
        db.execute(
            "UPDATE tasks SET state=?, payload=? WHERE id=?",
            (record.state.value, record.model_dump_json(), record.id),
        )

    def _bound(self, context: RunContext, record: TaskRecord) -> None:
        """Refuse a caller whose binding is not the record's binding.

        Checked before the host authorizer runs: a foreign tenant, actor or
        application must not even be able to describe this record to its own policy.
        """
        if (
            record.application_id != context.application_id
            or record.tenant_id != context.scope.tenant_id
            or record.actor_id != context.actor.id
            or not set(record.resources).issubset(context.scope.resources)
        ):
            raise TaskUnavailable("task unavailable for this binding")

    def _access(self, context: RunContext, operation: TaskOperation, task_id: str) -> TaskRecord:
        """Authorize one operation against the current context and stored record."""
        self._key(context)
        with self._connect() as db:
            record = self._row(db, task_id)
        self._bound(context, record)
        if self.authorize(context, operation, record) is not True:
            raise TaskUnavailable("current task permission denied")
        return record

    def _lock(self, task_id: str) -> TextIOWrapper:
        """Take the per-task exclusive lock, or refuse a live worker.

        The lock file is never removed: unlinking a lock another process still holds
        would let a third process lock a fresh inode and believe it is alone.
        """
        handle = (self.path / f"{task_id}.lock").open("a")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise TaskConflict("task is claimed by another worker") from exc
        return handle

    def _owned(self, task_id: str) -> None:
        """Refuse a mutation from a worker that does not hold the claim."""
        if task_id not in self._locks:
            raise TaskConflict("task is claimed by another worker")

    def _release(self, task_id: str) -> None:
        handle = self._locks.pop(task_id, None)
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    # -- public contract --------------------------------------------------------

    def create(
        self,
        context: RunContext,
        *,
        kind: str,
        request_id: str,
        business_key: str,
        effect_class: ToolEffectClass = ToolEffectClass.NON_IDEMPOTENT,
        progress_total: int = 0,
    ) -> TaskRecord:
        """Create the record for one request identity, or return the existing one.

        The default effect class is the strictest one: a task is only retried when
        the host declares that repeating it under the same business key is safe.
        """
        self._key(context)
        if self.authorize(context, TaskOperation.CREATE, None) is not True:
            raise TaskUnavailable("current task permission denied")
        clean_kind = self._identity(kind, name="kind", limit=MAX_KIND_CHARS)
        clean_request = self._identity(request_id, name="request_id", limit=MAX_REQUEST_ID_CHARS)
        clean_key = self._identity(business_key, name="business_key", limit=MAX_BUSINESS_KEY_CHARS)
        effect = _as_effect_class(effect_class)
        if type(progress_total) is not int or not 0 <= progress_total <= MAX_PROGRESS:
            raise ValueError("progress_total must be a bounded non-negative integer")
        task_id = _digest([context.application_id, context.scope.tenant_id, clean_request])
        now = self._now()
        with self._connect() as db:
            retired = db.execute(
                "SELECT 1 FROM task_tombstones WHERE id=? OR "
                "(application_id=? AND tenant_id=? AND business_key=?)",
                (task_id, context.application_id, context.scope.tenant_id, clean_key),
            ).fetchone()
            if retired is not None:
                raise TaskUnavailable("task identity was deleted and cannot be reused")
            existing = db.execute(
                "SELECT payload FROM tasks WHERE application_id=? AND tenant_id=? AND request_id=?",
                (context.application_id, context.scope.tenant_id, clean_request),
            ).fetchone()
            if existing is not None:
                record = TaskRecord.model_validate_json(existing[0])
                self._bound(context, record)
                if self.authorize(context, TaskOperation.READ, record) is not True:
                    raise TaskUnavailable("current task permission denied")
                if (
                    record.business_key != clean_key
                    or record.kind != clean_kind
                    or record.effect_class is not effect
                ):
                    # The same request key must mean the same request; silently
                    # reusing the identity for other work would hide a host bug.
                    raise TaskConflict("request identity reused for a different task")
                return record
            clash = db.execute(
                "SELECT id FROM tasks WHERE application_id=? AND tenant_id=? AND business_key=?",
                (context.application_id, context.scope.tenant_id, clean_key),
            ).fetchone()
            if clash is not None:
                raise TaskConflict("business key already belongs to another task")
            record = TaskRecord(
                id=task_id,
                kind=clean_kind,
                request_id=clean_request,
                business_key=clean_key,
                state=TaskState.QUEUED,
                effect_class=effect,
                progress_total=progress_total,
                application_id=context.application_id,
                tenant_id=context.scope.tenant_id,
                actor_id=context.actor.id,
                resources=context.scope.resources,
                created_at=now,
                updated_at=now,
            )
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.application_id,
                    record.tenant_id,
                    record.actor_id,
                    record.request_id,
                    record.business_key,
                    record.kind,
                    record.state.value,
                    record.created_at.isoformat(),
                    record.model_dump_json(),
                ),
            )
        return record

    def get(self, context: RunContext, task_id: str) -> TaskRecord:
        """Return one record the caller is bound to and currently permitted to read."""
        return self._access(context, TaskOperation.READ, task_id)

    def list(
        self,
        context: RunContext,
        *,
        state: TaskState | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> tuple[TaskRecord, ...]:
        """List the caller's own records, newest identity order last.

        Scoped to the caller's application, tenant and actor: a listing has no
        per-record binding to check, so the scope is the filter.
        """
        self._key(context)
        if self.authorize(context, TaskOperation.READ, None) is not True:
            raise TaskUnavailable("current task permission denied")
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        target = None if state is None else _as_state(state)
        pattern = None if kind is None else self._identity(kind, name="kind", limit=MAX_KIND_CHARS)
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT payload FROM tasks
                WHERE application_id=? AND tenant_id=? AND actor_id=?
                    AND (? IS NULL OR state=?) AND (? IS NULL OR kind=?)
                ORDER BY created_at, id LIMIT ?
                """,
                (
                    context.application_id,
                    context.scope.tenant_id,
                    context.actor.id,
                    target,
                    target,
                    pattern,
                    pattern,
                    limit,
                ),
            ).fetchall()
        visible: list[TaskRecord] = []
        for row in rows:
            record = TaskRecord.model_validate_json(row[0])
            try:
                self._bound(context, record)
            except TaskUnavailable:
                continue
            if self.authorize(context, TaskOperation.READ, record) is True:
                visible.append(record)
        return tuple(visible)

    def claim(self, context: RunContext, task_id: str) -> TaskRecord:
        """Move one queued task to running under this store's exclusive claim.

        The claim is held across calls, until the task settles or is reconciled, so
        a second worker cannot advance the same record even in the same process.
        """
        record = self._access(context, TaskOperation.RUN, task_id)
        if record.state is not TaskState.QUEUED:
            raise TaskConflict("only a queued task can be claimed")
        handle = self._lock(task_id)
        try:
            with self._connect() as db:
                latest = self._row(db, task_id)
                if latest.attempts >= self.max_attempts:
                    raise TaskConflict("attempt budget is exhausted")
                if latest.state is not TaskState.QUEUED:
                    # A cancel or reconcile landed between the read and the lock.
                    raise TaskConflict("task left the queue before it could be claimed")
                updated = latest.model_copy(
                    update={"state": TaskState.RUNNING, "updated_at": self._now()}
                )
                self._write(db, updated)
        except BaseException:
            handle.close()
            raise
        self._locks[task_id] = handle
        return updated

    def progress(
        self, context: RunContext, task_id: str, *, done: int, total: int | None = None
    ) -> TaskRecord:
        """Record monotonic progress for a running task owned by this worker."""
        self._access(context, TaskOperation.RUN, task_id)
        if type(done) is not int or not 0 <= done <= MAX_PROGRESS:
            raise ValueError("progress done must be a bounded non-negative integer")
        if total is not None and (type(total) is not int or not 0 <= total <= MAX_PROGRESS):
            raise ValueError("progress total must be a bounded non-negative integer")
        with self._connect() as db:
            record = self._row(db, task_id)
            # The settled check comes first so a finished task reports why it is
            # finished, not which worker used to own it.
            if record.state is not TaskState.RUNNING:
                raise TaskConflict("only a running task can report progress")
            self._owned(task_id)
            if done < record.progress_done:
                raise TaskConflict("progress must be monotonic")
            declared = record.progress_total if total is None else total
            if declared and done > declared:
                raise TaskConflict("progress exceeds the declared total")
            updated = record.model_copy(
                update={
                    "progress_done": done,
                    "progress_total": declared,
                    "updated_at": self._now(),
                }
            )
            self._write(db, updated)
        return updated

    def complete(
        self, context: RunContext, task_id: str, *, result: str | None = None
    ) -> TaskRecord:
        """Settle a running task as succeeded, counting the finished attempt.

        A cancel request that arrived while the handler was finishing does not turn
        this into a cancellation: the unit ran, and reporting a rollback that never
        happened would be a lie.
        """
        self._access(context, TaskOperation.RUN, task_id)
        clean = self._result(result)
        with self._connect() as db:
            record = self._row(db, task_id)
            if record.state is not TaskState.RUNNING:
                raise TaskConflict("only a running task can complete")
            self._owned(task_id)
            updated = record.model_copy(
                update={
                    "state": TaskState.SUCCEEDED,
                    "result": clean,
                    "error": None,
                    "error_code": None,
                    "attempts": record.attempts + 1,
                    "updated_at": self._now(),
                }
            )
            self._write(db, updated)
        self._release(task_id)
        return updated

    def fail(
        self, context: RunContext, task_id: str, *, error: str, error_code: ToolErrorCode
    ) -> TaskRecord:
        """Settle a failed attempt through ``decide_retry``.

        ``RETRY`` returns the task to the queue with the same identity and business
        key, ``FAIL`` settles it as failed, and ``INDETERMINATE`` becomes
        ``needs_reconciliation`` — never a replay of a non-idempotent write.
        """
        self._access(context, TaskOperation.RUN, task_id)
        message = self._payload(error, name="error", limit=MAX_ERROR_CHARS)
        code = _as_error_code(error_code)
        with self._connect() as db:
            record = self._row(db, task_id)
            if record.state is not TaskState.RUNNING:
                raise TaskConflict("only a running task can fail")
            self._owned(task_id)
            updated = self._settle_failure(record, message, code)
            self._write(db, updated)
        self._release(task_id)
        return updated

    def request_cancel(self, context: RunContext, task_id: str) -> TaskRecord:
        """Queue a cancel request, or settle a task whose work never started.

        Deliberately takes no claim lock: the request has to be able to arrive while
        the runner holds the task, which is exactly when it matters.
        """
        self._access(context, TaskOperation.CANCEL, task_id)
        with self._connect() as db:
            record = self._row(db, task_id)
            if record.state is TaskState.QUEUED:
                # Nothing started, so cancelling is exact and no handler will run.
                updated = record.model_copy(
                    update={
                        "state": TaskState.CANCELLED,
                        "cancel_requested": True,
                        "error": CANCEL_NOTE,
                        "error_code": None,
                        "updated_at": self._now(),
                    }
                )
            elif record.state is TaskState.RUNNING:
                # The runner stops at its next safe point; the state stays running
                # until it does, because a claim is not proof that nothing was
                # written.
                updated = record.model_copy(
                    update={"cancel_requested": True, "updated_at": self._now()}
                )
            else:
                raise TaskConflict(
                    "task is settled or awaits reconciliation and cannot be cancelled"
                )
            self._write(db, updated)
        return updated

    def reconcile(
        self, context: RunContext, task_id: str, *, state: TaskState, result: str | None = None
    ) -> TaskRecord:
        """Settle a task from the host's confirmed outcome, without re-running it.

        Used for restart recovery (the host checks staged files, revisions and index
        rows) and for the runner's own cancel stop. A requeue is the host's explicit
        statement that nothing was written, so it charges the interrupted attempt to
        the budget instead of granting unlimited crash retries.
        """
        self._access(context, TaskOperation.RUN, task_id)
        target = _as_state(state)
        if target not in _RECONCILABLE:
            raise ValueError("reconcile cannot put a task into running; claim does that")
        clean = self._result(result)
        if clean is not None and target is not TaskState.SUCCEEDED:
            raise ValueError("result is only recorded when reconciling to succeeded")
        handle = self._locks.pop(task_id, None)
        if handle is None:
            # Refuse while a live worker (another process) still holds the task; a
            # crashed worker's lock is already gone, which is the recovery case.
            handle = self._lock(task_id)
        try:
            with self._connect() as db:
                record = self._row(db, task_id)
                if record.state is TaskState.QUEUED or record.terminal:
                    raise TaskConflict("only a running or unreconciled task can be reconciled")
                updated = self._reconciled(record, target, clean)
                self._write(db, updated)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        return updated

    def delete(self, context: RunContext, task_id: str) -> None:
        """Remove a queued or settled record; never one whose outcome is unknown.

        A payload-free tombstone retires both request and business identities, so
        deletion can never authorize replaying a completed external write.
        """
        self._access(context, TaskOperation.DELETE, task_id)
        with self._connect() as db:
            record = self._row(db, task_id)
            if record.state not in _TERMINAL and record.state is not TaskState.QUEUED:
                raise TaskConflict("only a queued or settled task can be deleted; reconcile first")
            db.execute("PRAGMA secure_delete=ON")
            db.execute(
                "INSERT INTO task_tombstones VALUES(?,?,?,?)",
                (record.id, record.application_id, record.tenant_id, record.business_key),
            )
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def has_active_business_key(self, application_id: str, tenant_id: str, key: str) -> bool:
        """Host retention predicate; does not expose task content or grant access."""
        with self._connect() as db:
            row = db.execute(
                "SELECT state FROM tasks WHERE application_id=? AND tenant_id=? AND business_key=?",
                (application_id, tenant_id, key),
            ).fetchone()
        return row is not None and TaskState(row[0]) not in _TERMINAL

    def recoverable(self) -> tuple[TaskRecord, ...]:
        """Return non-terminal records for a restart scan, with no caller context.

        A restart scan runs before a request exists, so it cannot authorize: it
        returns records for the host to resolve with a fresh `RunContext` through
        `reconcile`, and it never re-executes anything itself.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM tasks WHERE state IN (?,?) ORDER BY created_at, id",
                _RECOVERABLE,
            ).fetchall()
        return tuple(TaskRecord.model_validate_json(row[0]) for row in rows)

    # -- transitions ------------------------------------------------------------

    def _settle_failure(self, record: TaskRecord, message: str, code: ToolErrorCode) -> TaskRecord:
        """Map one failed attempt onto the next durable state via `decide_retry`."""
        decision = decide_retry(
            effect_class=record.effect_class,
            error_code=code,
            attempt_no=record.attempts + 1,
            max_attempts=self.max_attempts,
        )
        now = self._now()
        attempts = record.attempts + 1
        if record.cancel_requested:
            # A cancel request outranks a retry: the caller asked to stop, so an
            # idempotent timeout is not replayed. Only a genuinely unknown outcome
            # survives as `needs_reconciliation`.
            if decision is RetryDecision.INDETERMINATE:
                return record.model_copy(
                    update={
                        "state": TaskState.NEEDS_RECONCILIATION,
                        "attempts": attempts,
                        "error": f"{message}; {CANCEL_NOTE}",
                        "error_code": code,
                        "updated_at": now,
                    }
                )
            return record.model_copy(
                update={
                    "state": TaskState.CANCELLED,
                    "attempts": attempts,
                    "error": f"{CANCEL_NOTE}; last error: {message}",
                    "error_code": None,
                    "updated_at": now,
                }
            )
        state = {
            RetryDecision.RETRY: TaskState.QUEUED,
            RetryDecision.FAIL: TaskState.FAILED,
            RetryDecision.INDETERMINATE: TaskState.NEEDS_RECONCILIATION,
        }[decision]
        return record.model_copy(
            update={
                "state": state,
                "attempts": attempts,
                "error": message,
                "error_code": code,
                "updated_at": now,
            }
        )

    def _reconciled(self, record: TaskRecord, target: TaskState, result: str | None) -> TaskRecord:
        """Apply a host-confirmed outcome to a record with an open attempt."""
        now = self._now()
        attempts = record.attempts
        if record.state is TaskState.RUNNING:
            # Claiming started a physical attempt, so a confirmed terminal outcome
            # counts it; a requeue charges it too, so crashes are not free retries.
            attempts += 1
        if target is TaskState.QUEUED:
            if attempts >= self.max_attempts:
                raise TaskConflict("attempt budget is exhausted; reconcile to a terminal state")
            return record.model_copy(
                update={"state": target, "attempts": attempts, "updated_at": now}
            )
        if target is TaskState.SUCCEEDED:
            return record.model_copy(
                update={
                    "state": target,
                    "attempts": attempts,
                    "result": result,
                    "error": None,
                    "error_code": None,
                    "updated_at": now,
                }
            )
        if target is TaskState.CANCELLED:
            return record.model_copy(
                update={
                    "state": target,
                    "attempts": attempts,
                    "cancel_requested": True,
                    "error": CANCEL_NOTE,
                    "error_code": None,
                    "updated_at": now,
                }
            )
        if target is TaskState.FAILED:
            return record.model_copy(
                update={
                    "state": target,
                    "attempts": attempts,
                    "error": record.error or "reconciled: the host confirmed the attempt failed",
                    "updated_at": now,
                }
            )
        return record.model_copy(
            update={
                "state": target,
                "attempts": attempts,
                "error": record.error or "reconciled: the host could not confirm the outcome",
                "updated_at": now,
            }
        )


class TaskRunner:
    """Execute claimed tasks through host handlers, one unit of work at a time."""

    def __init__(self, store: TaskStore, *, handlers: tuple[TaskHandler, ...]) -> None:
        if not handlers:
            raise ValueError("at least one task handler is required")
        registry: dict[str, TaskHandler] = {}
        for handler in handlers:
            kind: object = getattr(handler, "kind", None)
            if not isinstance(kind, str) or not kind.strip() or len(kind) > MAX_KIND_CHARS:
                raise ValueError("every task handler must declare a bounded non-empty kind")
            if kind in registry:
                raise ValueError(f"duplicate task handler kind: {kind!r}")
            if not callable(getattr(handler, "run", None)) or not inspect.iscoroutinefunction(
                handler.run
            ):
                raise ValueError(f"task handler {kind!r} must define an async run method")
            registry[kind] = handler
        self.store = store
        self._handlers = registry

    async def run_once(self, context: RunContext, task_id: str) -> TaskRecord:
        """Run one unit; on cancellation or revocation leave durable reconciliation evidence."""
        previous_claim = self.store._locks.get(task_id)
        try:
            return await self._run_once(context, task_id)
        finally:
            if self.store._locks.get(task_id) is not previous_claim:
                self.store._release(task_id)

    async def _run_once(self, context: RunContext, task_id: str) -> TaskRecord:
        """Claim one queued task, run its handler, and settle the durable record.

        A handler is one unit of work. The runner honors a cancel request immediately
        before that unit starts; mid-flight it can only observe the outcome, because
        a coroutine cannot be preempted. A handler that returns normally finished its
        unit, so the record is settled as succeeded even when a cancel request
        arrived too late to be a rollback.
        """
        record = self.store.get(context, task_id)
        handler = self._handlers.get(record.kind)
        if handler is None:
            # Refused before the claim so a task no worker can serve is never left
            # running with an attempt nobody settled.
            raise TaskConflict(f"no handler registered for task kind {record.kind!r}")
        claimed = self.store.claim(context, task_id)
        if claimed.cancel_requested:
            # A cancel request that survived a requeue: no unit of work has started,
            # so cancelling is exact and the handler is never invoked.
            return self.store.reconcile(context, task_id, state=TaskState.CANCELLED)
        try:
            bound_context = context.model_copy(
                update={"scope": context.scope.model_copy(update={"resources": claimed.resources})}
            )
            outcome = await handler.run(bound_context, claimed)
            if not isinstance(outcome, TaskOutcome):
                raise ValueError("handler must return a TaskOutcome")
            outcome = TaskOutcome.model_validate(outcome.model_dump())
        except TimeoutError as exc:
            return self.store.fail(
                context, task_id, error=_diagnostic(exc), error_code=ToolErrorCode.TIMEOUT
            )
        except Exception as exc:
            return self.store.fail(
                context,
                task_id,
                error=_diagnostic(exc),
                error_code=ToolErrorCode.TOOL_EXECUTION_ERROR,
            )
        latest = self.store.get(context, task_id)
        if latest.state is not TaskState.RUNNING:
            # A handler that settled its own record is tolerated: the durable state
            # wins and the runner never overwrites a settled task.
            return latest
        if outcome.progress_done is not None:
            try:
                self.store.progress(
                    context,
                    task_id,
                    done=outcome.progress_done,
                    total=outcome.progress_total,
                )
            except ValueError as exc:
                return self.store.fail(
                    context,
                    task_id,
                    error=_diagnostic(exc),
                    error_code=ToolErrorCode.TOOL_EXECUTION_ERROR,
                )
        try:
            return self.store.complete(context, task_id, result=outcome.result)
        except ValueError as exc:
            # An unusable outcome is a handler contract violation, so it settles
            # through the failure path rather than leaving a task running forever.
            return self.store.fail(
                context,
                task_id,
                error=_diagnostic(exc),
                error_code=ToolErrorCode.TOOL_EXECUTION_ERROR,
            )

    async def drain(self, context: RunContext, *, limit: int = 10) -> tuple[TaskRecord, ...]:
        """Run at most ``limit`` queued tasks this worker can serve.

        A task whose kind has no registered handler stays queued for a worker that
        can serve it, and a task claimed by another worker is skipped: the caller
        sees exactly the records this run settled.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_DRAIN_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_DRAIN_LIMIT}")
        settled: list[TaskRecord] = []
        for record in self.store.list(context, state=TaskState.QUEUED, limit=limit):
            if record.kind not in self._handlers:
                continue
            try:
                settled.append(await self.run_once(context, record.id))
            except TaskConflict:
                continue
        return tuple(settled)

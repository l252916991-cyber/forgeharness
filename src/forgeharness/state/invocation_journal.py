"""Durable journal of tool invocations, and the recovery decision it drives.

This is the **source of truth for tool side effects**. The checkpoint owns
control-flow state and the trace is observable evidence; neither records whether a
side effect happened, and a missing `tool.completed` event must never be read as
"the tool did not run".

The module deliberately holds two separate responsibilities:

* ``decide_recovery`` is the **state-machine semantics** the runtime loop applies.
* ``SQLiteInvocationJournal`` is **durability**: transactions, compare-and-swap,
  and fsync. No SQL lives in the runtime loop, so adding retry later cannot turn
  `loop.py` into a database scheduler.

Scope note: recovery here is fail-closed, not exactly-once. Between ``started``
and the actual side effect there is a window the journal cannot observe, so a
non-idempotent call in that window is never replayed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.state.sqlite import connect_wal
from forgeharness.tools.base import ToolEffectClass, ToolOutput

JOURNAL_VERSION = 1

# A result larger than this must not be silently truncated into the journal, because
# recovery has to reconstruct the transcript exactly. The dispatcher already bounds
# tool output, so this is a guard rather than a normal path.
MAX_JOURNALED_RESULT_BYTES = 2_000_000


class InvocationState(StrEnum):
    """Lifecycle of one logical tool call."""

    # Accepted for execution; the tool has NOT started. Created after policy and
    # approval, immediately before execution.
    CLAIMED = "claimed"
    # Durably committed. Only now may the runtime call the tool.
    STARTED = "started"
    # The tool returned and the full canonical ToolOutput is persisted.
    COMPLETED = "completed"
    # The runtime has evidence that recovery cannot duplicate an uncertain side
    # effect. An exception alone is NOT such evidence.
    FAILED = "failed"
    # Whether the side effect occurred cannot be established.
    INDETERMINATE = "indeterminate"

    @property
    def terminal(self) -> bool:
        """True once the invocation can no longer change state."""
        return self is not InvocationState.CLAIMED and self is not InvocationState.STARTED


class RecoveryDecision(StrEnum):
    """What recovery does with a journal row found at resume."""

    # Nothing was executed yet, so the call may proceed.
    EXECUTE = "execute"
    # The recorded result is reused; the tool is not called again.
    REUSE_RESULT = "reuse_result"
    # A read-only tool may safely run again.
    REPLAY_READ_ONLY = "replay_read_only"
    # An idempotent tool may run again under the same key.
    REPLAY_IDEMPOTENT = "replay_idempotent"
    # Never replay: the side effect's occurrence is unknown, or M10-A does not retry.
    REFUSE_REPLAY = "refuse_replay"


class InvocationRecord(FrozenModel):
    """One logical tool call and everything recovery needs about it."""

    invocation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    logical_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    effect_class: ToolEffectClass
    args_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1)
    state: InvocationState
    attempt_count: int = Field(default=0, ge=0)
    claimed_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # The canonical ToolOutput, stored whole: recovery must return the exact
    # observation the model saw, not re-derive it by running the tool again.
    result: ToolOutput | None = None
    result_digest: str | None = None
    result_size_bytes: int = Field(default=0, ge=0)
    result_storage_kind: str = "inline"
    error_code: str | None = None
    checkpoint_revision: int = Field(default=0, ge=0)
    recovery_decision: RecoveryDecision | None = None
    journal_version: int = JOURNAL_VERSION


class AttemptOutcome(StrEnum):
    """How one physical execution ended."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class AttemptRecord(FrozenModel):
    """One physical execution of a logical call.

    Kept separate from the invocation because retry makes the two levels differ: one
    logical call may be attempted several times. The attempt records *what happened
    when*, and never stores the tool result — the canonical result belongs to the
    invocation, so there is exactly one copy of it.
    """

    attempt_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime | None = None
    outcome: AttemptOutcome | None = None
    error_code: str | None = None
    # The ceiling that applied to this attempt, and the delay paid before it (0 for
    # the first), so latency evidence can explain a slow logical call.
    timeout_seconds: float = Field(gt=0)
    backoff_before_ms: int = Field(default=0, ge=0)


class JournalError(RuntimeError):
    """The journal could not record a state transition safely."""


def decide_recovery(record: InvocationRecord) -> RecoveryDecision:
    """Decide what to do with a journal row found during recovery.

    The matrix is frozen in ADR 009. Two properties matter most:

    * ``started`` with a non-idempotent effect is refused, because the journal
      cannot tell whether the side effect occurred inside that window. Refusing
      there is the whole point of the design.
    * ``failed`` terminates in M10-A rather than replaying; retry policy is M10-B's
      decision and is made from `error_code` plus recoverability, not here.
    """
    if record.state is InvocationState.COMPLETED:
        return RecoveryDecision.REUSE_RESULT
    if record.state is InvocationState.INDETERMINATE:
        return RecoveryDecision.REFUSE_REPLAY
    if record.state is InvocationState.FAILED:
        return RecoveryDecision.REFUSE_REPLAY
    if record.state is InvocationState.CLAIMED:
        # Nothing has run, so execution is safe for every effect class.
        return RecoveryDecision.EXECUTE
    # STARTED: the side effect may or may not have happened.
    if record.effect_class is ToolEffectClass.READ_ONLY:
        return RecoveryDecision.REPLAY_READ_ONLY
    if record.effect_class is ToolEffectClass.IDEMPOTENT:
        return RecoveryDecision.REPLAY_IDEMPOTENT
    return RecoveryDecision.REFUSE_REPLAY


def terminal_state_for_failure(
    *, effect_class: ToolEffectClass, execution_attempted: bool
) -> InvocationState:
    """Classify a failure by side-effect certainty, never by the exception.

    Execution is what creates uncertainty. A failure raised before execution began
    (schema, policy, unknown tool) cannot have had a side effect; a failure after
    execution began is only unambiguously ``failed`` when the effect class makes a
    repeat safe. A non-idempotent tool that started is ``indeterminate`` because the
    runtime cannot know how far it got.
    """
    if not execution_attempted:
        return InvocationState.FAILED
    if effect_class in (ToolEffectClass.READ_ONLY, ToolEffectClass.IDEMPOTENT):
        return InvocationState.FAILED
    return InvocationState.INDETERMINATE


def settle_invocation(
    record: InvocationRecord,
    output: ToolOutput,
    now: datetime,
    *,
    failed_state: InvocationState = InvocationState.FAILED,
) -> InvocationRecord:
    """Persist the canonical result and the terminal state of a logical call.

    ``failed_state`` is chosen by the caller, which is where the retry decision
    lives: it is `FAILED` when the failure is unambiguous and `INDETERMINATE` when a
    non-idempotent effect may have happened. Deriving it here instead would duplicate
    the retry rules in a second place.
    """
    state = InvocationState.COMPLETED if output.ok else failed_state
    code = output.error.code.value if output.error is not None else None
    payload = output.model_dump_json()
    return record.model_copy(
        update={
            "state": state,
            "result": output,
            "result_digest": _digest_output(output),
            "result_size_bytes": len(payload.encode()),
            "error_code": code,
            "finished_at": now,
        }
    )


def args_digest(arguments: dict[str, object]) -> str:
    """Hash canonicalised arguments so a call's identity is stable and comparable."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def idempotency_key_for(*, run_id: str, logical_call_id: str) -> str:
    """Derive the stable key for one logical call.

    Derived deterministically from stable identity so recovery reconstructs the same
    key without persisting a separate secret. Regenerating it on resume is
    forbidden, which is why it is a pure function of values that do not change.
    """
    return f"{run_id}:{logical_call_id}"


def _digest_output(output: ToolOutput) -> str:
    return hashlib.sha256(output.model_dump_json().encode()).hexdigest()


class InvocationJournal(Protocol):
    """Durable store for tool invocation state."""

    def claim(self, record: InvocationRecord) -> InvocationRecord:
        """Persist a new invocation in `claimed`. Fails if the call is already known."""
        ...

    def mark_started(self, invocation_id: str, *, checkpoint_revision: int) -> InvocationRecord:
        """Durably commit `started` before any side effect occurs."""
        ...

    def settle(self, record: InvocationRecord) -> InvocationRecord:
        """Persist a terminal state together with its canonical result."""
        ...

    def load(self, *, run_id: str, logical_call_id: str) -> InvocationRecord | None:
        """Return the row for one logical call, if it has entered execution."""
        ...

    def pending_for_run(self, run_id: str) -> tuple[InvocationRecord, ...]:
        """Return non-terminal invocations for a run, for resume-time recovery."""
        ...

    def record_recovery_decision(
        self, invocation_id: str, *, state: InvocationState, decision: RecoveryDecision
    ) -> None:
        """Persist what recovery decided, so a resumed run's choice is auditable."""
        ...

    def start_attempt(self, attempt: AttemptRecord) -> AttemptRecord:
        """Record that one physical execution is about to begin."""
        ...

    def finish_attempt(self, attempt: AttemptRecord) -> AttemptRecord:
        """Record how one physical execution ended."""
        ...

    def attempts_for(self, invocation_id: str) -> tuple[AttemptRecord, ...]:
        """Return every attempt for one logical call, in order."""
        ...


class SQLiteInvocationJournal:
    """SQLite-backed invocation journal sharing the checkpoint database.

    Transitions are single-statement updates guarded by the current state, so two
    processes cannot both advance one invocation.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_invocations (
                    invocation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    logical_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    effect_class TEXT NOT NULL,
                    args_digest TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result TEXT,
                    result_digest TEXT,
                    result_size_bytes INTEGER NOT NULL DEFAULT 0,
                    result_storage_kind TEXT NOT NULL DEFAULT 'inline',
                    error_code TEXT,
                    checkpoint_revision INTEGER NOT NULL DEFAULT 0,
                    recovery_decision TEXT,
                    journal_version INTEGER NOT NULL,
                    UNIQUE (run_id, logical_call_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tool_invocations_run_idx "
                "ON tool_invocations (run_id, state)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    invocation_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    outcome TEXT,
                    error_code TEXT,
                    timeout_seconds REAL NOT NULL,
                    backoff_before_ms INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (invocation_id, attempt_no),
                    FOREIGN KEY (invocation_id)
                        REFERENCES tool_invocations (invocation_id)
                )
                """
            )

    def claim(self, record: InvocationRecord) -> InvocationRecord:
        """Insert a new invocation. A repeat of the same logical call is an error."""
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO tool_invocations (
                        invocation_id, run_id, logical_call_id, tool_name, effect_class,
                        args_digest, idempotency_key, state, attempt_count, claimed_at,
                        result_size_bytes, result_storage_kind, checkpoint_revision,
                        journal_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.invocation_id,
                        record.run_id,
                        record.logical_call_id,
                        record.tool_name,
                        record.effect_class.value,
                        record.args_digest,
                        record.idempotency_key,
                        record.state.value,
                        record.attempt_count,
                        record.claimed_at.isoformat(),
                        record.result_size_bytes,
                        record.result_storage_kind,
                        record.checkpoint_revision,
                        record.journal_version,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise JournalError(
                    f"invocation already claimed: {record.run_id}/{record.logical_call_id}"
                ) from exc
        return record

    def mark_started(self, invocation_id: str, *, checkpoint_revision: int) -> InvocationRecord:
        """Commit `started` durably. The caller must not dispatch before this returns."""
        now = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tool_invocations
                SET state = ?, started_at = ?, attempt_count = attempt_count + 1,
                    checkpoint_revision = ?
                WHERE invocation_id = ? AND state = ?
                """,
                (
                    InvocationState.STARTED.value,
                    now.isoformat(),
                    checkpoint_revision,
                    invocation_id,
                    InvocationState.CLAIMED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(f"cannot start invocation {invocation_id}: not claimed")
            connection.commit()
        loaded = self._load_by_id(invocation_id)
        if loaded is None:
            raise JournalError(f"invocation vanished after start: {invocation_id}")
        return loaded

    def settle(self, record: InvocationRecord) -> InvocationRecord:
        """Persist a terminal state and its canonical result in one guarded update."""
        if record.state is InvocationState.CLAIMED or record.state is InvocationState.STARTED:
            raise JournalError(f"refusing to settle a non-terminal state: {record.state.value}")
        payload = record.result.model_dump_json() if record.result is not None else None
        if payload is not None and len(payload.encode()) > MAX_JOURNALED_RESULT_BYTES:
            # Never truncate: a shortened result could not reconstruct the transcript.
            raise JournalError(
                "journaled result exceeds MAX_JOURNALED_RESULT_BYTES; the tool output "
                "contract must bound its own output"
            )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tool_invocations
                SET state = ?, finished_at = ?, result = ?, result_digest = ?,
                    result_size_bytes = ?, error_code = ?, attempt_count = ?
                WHERE invocation_id = ? AND state IN (?, ?)
                """,
                (
                    record.state.value,
                    (record.finished_at or datetime.now(UTC)).isoformat(),
                    payload,
                    record.result_digest,
                    record.result_size_bytes,
                    record.error_code,
                    record.attempt_count,
                    record.invocation_id,
                    InvocationState.CLAIMED.value,
                    InvocationState.STARTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(
                    f"cannot settle invocation {record.invocation_id}: already terminal"
                )
            connection.commit()
        return record

    def load(self, *, run_id: str, logical_call_id: str) -> InvocationRecord | None:
        """Return one logical call's row, or None when it never entered execution."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE run_id = ? AND logical_call_id = ?",
                (run_id, logical_call_id),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def pending_for_run(self, run_id: str) -> tuple[InvocationRecord, ...]:
        """Return invocations still in `claimed` or `started`, ordered by claim time."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tool_invocations
                WHERE run_id = ? AND state IN (?, ?)
                ORDER BY claimed_at
                """,
                (run_id, InvocationState.CLAIMED.value, InvocationState.STARTED.value),
            ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def record_recovery_decision(
        self, invocation_id: str, *, state: InvocationState, decision: RecoveryDecision
    ) -> None:
        """Persist what recovery decided, so a resumed run's choice is auditable."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tool_invocations
                SET state = ?, recovery_decision = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE invocation_id = ?
                """,
                (state.value, decision.value, datetime.now(UTC).isoformat(), invocation_id),
            )
            connection.commit()

    def start_attempt(self, attempt: AttemptRecord) -> AttemptRecord:
        """Insert one attempt row. A repeated attempt number is an error."""
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO tool_attempts (
                        attempt_id, invocation_id, attempt_no, started_at,
                        timeout_seconds, backoff_before_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.attempt_id,
                        attempt.invocation_id,
                        attempt.attempt_no,
                        attempt.started_at.isoformat(),
                        attempt.timeout_seconds,
                        attempt.backoff_before_ms,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise JournalError(
                    f"attempt already recorded: {attempt.invocation_id}#{attempt.attempt_no}"
                ) from exc
        return attempt

    def finish_attempt(self, attempt: AttemptRecord) -> AttemptRecord:
        """Record an attempt's outcome and error code."""
        if attempt.outcome is None:
            raise JournalError("an attempt must have an outcome before it is finished")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tool_attempts
                SET finished_at = ?, outcome = ?, error_code = ?
                WHERE attempt_id = ? AND outcome IS NULL
                """,
                (
                    (attempt.finished_at or datetime.now(UTC)).isoformat(),
                    attempt.outcome.value,
                    attempt.error_code,
                    attempt.attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(f"cannot finish attempt {attempt.attempt_id}")
            connection.commit()
        return attempt

    def attempts_for(self, invocation_id: str) -> tuple[AttemptRecord, ...]:
        """Return one invocation's attempts in execution order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_attempts WHERE invocation_id = ? ORDER BY attempt_no",
                (invocation_id,),
            ).fetchall()
        return tuple(_row_to_attempt(row) for row in rows)

    def _load_by_id(self, invocation_id: str) -> InvocationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def _connect(self) -> sqlite3.Connection:
        connection = connect_wal(self._path, foreign_keys=True)
        # Named access keeps the row mapping explicit; positional unpacking would
        # silently break if the schema gains a column.
        connection.row_factory = sqlite3.Row
        return connection


def _row_to_record(row: sqlite3.Row) -> InvocationRecord:
    """Rebuild an InvocationRecord from a SQLite row by column name."""
    result_payload = row["result"]
    return InvocationRecord(
        invocation_id=str(row["invocation_id"]),
        run_id=str(row["run_id"]),
        logical_call_id=str(row["logical_call_id"]),
        tool_name=str(row["tool_name"]),
        effect_class=ToolEffectClass(str(row["effect_class"])),
        args_digest=str(row["args_digest"]),
        idempotency_key=str(row["idempotency_key"]),
        state=InvocationState(str(row["state"])),
        attempt_count=_as_int(row["attempt_count"]),
        claimed_at=datetime.fromisoformat(str(row["claimed_at"])),
        started_at=_as_datetime(row["started_at"]),
        finished_at=_as_datetime(row["finished_at"]),
        result=ToolOutput.model_validate_json(str(result_payload)) if result_payload else None,
        result_digest=str(row["result_digest"]) if row["result_digest"] else None,
        result_size_bytes=_as_int(row["result_size_bytes"]),
        result_storage_kind=str(row["result_storage_kind"]),
        error_code=str(row["error_code"]) if row["error_code"] else None,
        checkpoint_revision=_as_int(row["checkpoint_revision"]),
        recovery_decision=(
            RecoveryDecision(str(row["recovery_decision"])) if row["recovery_decision"] else None
        ),
        journal_version=_as_int(row["journal_version"]),
    )


def _row_to_attempt(row: sqlite3.Row) -> AttemptRecord:
    """Rebuild an attempt record from a row, by column name."""
    outcome = row["outcome"]
    return AttemptRecord(
        attempt_id=str(row["attempt_id"]),
        invocation_id=str(row["invocation_id"]),
        attempt_no=_as_int(row["attempt_no"]),
        started_at=datetime.fromisoformat(str(row["started_at"])),
        finished_at=_as_datetime(row["finished_at"]),
        outcome=AttemptOutcome(str(outcome)) if outcome else None,
        error_code=str(row["error_code"]) if row["error_code"] else None,
        timeout_seconds=float(row["timeout_seconds"]),
        backoff_before_ms=_as_int(row["backoff_before_ms"]),
    )


def _as_int(value: object) -> int:
    """Coerce a SQLite scalar to int without trusting its static type."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float, bytes)):
        return int(value)
    raise JournalError(f"unexpected journal integer value: {value!r}")


def _as_datetime(value: object) -> datetime | None:
    """Parse an optional ISO timestamp column."""
    return datetime.fromisoformat(str(value)) if value else None

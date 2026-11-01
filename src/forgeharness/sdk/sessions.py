"""Scoped local conversations composed over the public durable controllers.

Hosts authenticate each operation and supply live authorization and material
validation. Conversation text is untrusted context, never an execution identity.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from forgeharness.context.provider import ContextMaterial, ContextProvider
from forgeharness.domain.models import FrozenModel, ResourceScope, RunContext, RunResult, RunStatus
from forgeharness.observability.hash_chain import TraceEnvelope, verify_trace
from forgeharness.sdk.application import ApplicationSpec, Harness, ScopeError
from forgeharness.sdk.privacy import EvidenceTransform
from forgeharness.sdk.sources import SourceRef, governed_source_refs
from forgeharness.sdk.workflows import WorkflowResult, WorkflowRunner, WorkflowSpec
from forgeharness.state.approval import ApprovalGrant
from forgeharness.state.sqlite import connect_wal


class SessionConflict(ValueError):
    """A request conflicts with the current durable conversation state."""


class SessionUnavailable(ScopeError):
    """Session content or its supporting evidence is unavailable to this caller."""


class SessionOperation(StrEnum):
    CREATE = "create"
    READ = "read"
    SUBMIT = "submit"
    CLOSE = "close"
    INSPECT = "inspect"
    EVENTS = "events"
    APPROVE = "approve"
    RESUME = "resume"
    DELETE = "delete"


class Session(FrozenModel):
    id: str
    application_id: str
    application_version: str
    policy_version: str
    tenant_id: str
    actor_id: str
    resources: tuple[str, ...]
    scope: ResourceScope
    status: Literal["open", "closed", "deleted"] = "open"
    revision: int = Field(default=1, ge=1)
    active_turn: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: Literal[1] = 1


class SessionTurn(FrozenModel):
    id: str
    session_id: str
    sequence: int = Field(ge=1)
    request_id: str
    run_id: str
    workflow_id: str | None = None
    context: RunContext
    status: Literal["starting", "deleted"] | RunStatus = "starting"
    text: str
    output: str | None = None
    error: str | None = None
    materials: tuple[ContextMaterial, ...] = ()
    # Version-bound sources this turn read through governed tools. Identifiers only:
    # the content stays in the host's ledger and is revalidated before reuse (ADR 016).
    sources: tuple[SourceRef, ...] = ()
    history_turns: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: Literal[1] = 1


Authorize = Callable[[RunContext, SessionOperation, Session | None], bool]
ValidateSources = Callable[[RunContext, tuple[SourceRef, ...]], bool]
ValidateMaterials = Callable[[RunContext, tuple[ContextMaterial, ...]], bool]
Controller = Harness | WorkflowRunner
Result = RunResult | WorkflowResult
_ACTIVE = {"starting", RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL}
# History validation must stay exhaustive, so a chain is only as cheap as its own
# length allows. This bound keeps one operation from doing unbounded work on a
# pathologically long conversation: the window query reads at most this many
# earlier turns and a chain that reaches further is refused, never validated in
# part. Sessions below the bound are unaffected; a longer conversation starts a
# new session instead of relying on a partial check.
_MAX_VALIDATED_TURNS = 512


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _in_clause(count: int) -> str:
    """Placeholder list for a bounded IN query; every value stays bound."""
    return ",".join("?" * count)


class ValidationCounters:
    """Observable validation work of one service, for benchmarks and regression tests.

    Diagnostics only; no behaviour depends on them. ``controller_lookups`` counts
    evidence controllers built (a Harness is cached per session), ``turn_queries``
    turn-row reads (list, single and batched), ``run_lookups`` run evidence probes
    and ``turns_validated`` turns checked. A turn counts again only when its
    durable content changed, never once per referencing descendant.
    """

    def __init__(self) -> None:
        self.controller_lookups = 0
        self.turn_queries = 0
        self.run_lookups = 0
        self.turns_validated = 0
        self.source_checks = 0


class _Validation:
    """One public operation's ancestor walk state (STATE-003).

    The walk stays exhaustive, but each turn costs one row read and one run probe
    however many descendants reference it, and every ancestor read of an operation
    is batched into the window query instead of one query per ancestor.
    """

    def __init__(self) -> None:
        self.anchor = 0
        self.windowed = False
        self.loaded: dict[str, SessionTurn] = {}
        self.done: dict[str, SessionTurn] = {}
        self.runs: dict[str, Result | None] = {}


class _HistoryProvider:
    def __init__(
        self, service: SessionService, turn: SessionTurn, validation: _Validation | None = None
    ) -> None:
        self.service = service
        self.turn = turn
        # A provider can suspend while another operation revokes a source. Never
        # reuse permission conclusions from before that await at this boundary.
        del validation

    async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
        del task
        state = _Validation()
        latest = self.service._turn(self.turn.context, self.turn.id)
        self.service._access(context, SessionOperation.READ)
        # This provider is deliberately last. Recheck every material recorded by
        # earlier providers, and all history dependencies, immediately before the
        # compiled context can reach the model. A later provider may have awaited
        # after an earlier provider's own validation passed.
        self.service._validate_turn(context, latest, state)
        materials = []
        for previous in self.service._required_turns(latest.history_turns, state.loaded):
            materials.append(
                ContextMaterial(
                    id=f"session-history-{previous.id}",
                    source=f"session:{previous.session_id}/turn:{previous.id}",
                    version=_digest([previous.text, previous.output, previous.run_id]),
                    application_id=context.application_id,
                    tenant_id=context.scope.tenant_id,
                    score=float(previous.sequence),
                    content=json.dumps(
                        {
                            "kind": "untrusted_conversation_history",
                            "user": previous.text,
                            "assistant": previous.output,
                            "run_id": previous.run_id,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return tuple(materials)


class _RecordingProvider:
    def __init__(
        self, service: SessionService, turn: SessionTurn, provider: ContextProvider
    ) -> None:
        self.service = service
        self.turn = turn
        self.provider = provider

    async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
        materials = await self.provider.provide(task=task, context=context)
        clean = tuple(
            ContextMaterial.model_validate(
                self.service.transform.transform(item.model_dump(mode="json"))
            )
            for item in materials
        )
        self.service._validate_materials(context, clean)
        self.service._record_materials(self.turn, clean)
        return clean


class SessionService:
    """Single-machine, owner-scoped conversation controller.

    ``authorize`` is mandatory and must query current host permissions; supplying
    an always-true callback is suitable only for isolated examples. Providers need
    a ``validate_materials`` callback that checks each concrete source version and
    current access. Roles are refreshed for each new turn; suspended execution
    keeps its original binding and refuses recovery when the owner's roles change.
    ``counters`` accumulates observable validation work for benchmarks and
    regression tests; no behaviour depends on it.
    """

    def __init__(
        self,
        application: ApplicationSpec | WorkflowSpec,
        *,
        state_dir: Path,
        authorize: Authorize,
        validate_materials: ValidateMaterials | None = None,
        validate_sources: ValidateSources | None = None,
        max_message_chars: int = 100_000,
        history_limit: int = 20,
    ) -> None:
        if not callable(authorize):
            raise ValueError("a live host authorizer is required")
        if not 1 <= max_message_chars <= 1_000_000 or not 1 <= history_limit <= 100:
            raise ValueError("invalid message or history limit")
        apps = (
            (application,)
            if isinstance(application, ApplicationSpec)
            else tuple(step.application for step in application.steps)
        )
        if any(app.context_providers for app in apps) and validate_materials is None:
            raise ValueError("context providers require current material validation")
        governed = frozenset(
            tool.spec.name
            for app in apps
            for tool in app.tools
            if getattr(tool.spec, "governed_sources", False)
        )
        if governed and validate_sources is None:
            raise ValueError("governed source tools require current source validation")
        self.application = application
        self.authorize = authorize
        self.material_validator = validate_materials
        self.source_validator = validate_sources
        self.governed_tools = governed
        self.max_message_chars = max_message_chars
        self.history_limit = history_limit
        self.counters = ValidationCounters()
        self.transform: EvidenceTransform = application.evidence_transform
        self._lookups: dict[str, Controller] = {}
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.state_dir.stat().st_mode) & 0o077:
            raise ScopeError("session directory must be owner-private (mode 0700)")
        self.database = self.state_dir / "sessions.sqlite"
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS session_schema (version INTEGER NOT NULL)")
            versions = db.execute("SELECT version FROM session_schema").fetchall()
            if not versions:
                db.execute("INSERT INTO session_schema VALUES(1)")
            elif versions != [(1,)]:
                raise SessionConflict("unsupported session schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS sdk_sessions (id TEXT PRIMARY KEY, payload TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS sdk_turns (session_key TEXT, id TEXT, "
                "sequence INTEGER, request_id TEXT, digest TEXT, payload TEXT, "
                "PRIMARY KEY(session_key,id), UNIQUE(session_key,sequence), "
                "UNIQUE(session_key,request_id))"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(connect_wal(self.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _key(self, context: RunContext) -> str:
        RunContext.model_validate(context.model_dump())
        if (
            context.application_id != self.application.id
            or context.application_version != self.application.version
            or context.policy_version != self.application.policy_version
            or len(context.session_id) > 200
            or self.transform.transform(context.model_dump(mode="json"))
            != context.model_dump(mode="json")
        ):
            raise SessionUnavailable("invalid session binding or credentials in identity")
        return _digest([context.application_id, context.scope.tenant_id, context.session_id])

    @contextmanager
    def _operation(self, context: RunContext) -> Iterator[None]:
        with (self.state_dir / f"{self._key(context)}.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionConflict("session has an active worker") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _session(self, context: RunContext, *, allow_deleted: bool = False) -> Session:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM sdk_sessions WHERE id=?", (self._key(context),)
            ).fetchone()
        if row is None:
            raise SessionUnavailable("session unavailable")
        session = Session.model_validate_json(row[0])
        if session.status == "deleted" and not allow_deleted:
            raise SessionUnavailable("session deleted")
        return session

    def _authorize(
        self,
        context: RunContext,
        operation: SessionOperation,
        session: Session | None,
        *,
        delegated: bool = False,
    ) -> None:
        self._key(context)
        if session is not None and (
            context.session_id != session.id
            or context.application_id != session.application_id
            or context.application_version != session.application_version
            or context.policy_version != session.policy_version
            or context.scope != session.scope
            or (not delegated and context.actor.id != session.actor_id)
        ):
            raise SessionUnavailable("session unavailable for this binding")
        if self.authorize(context, operation, session) is not True:
            raise SessionUnavailable("current session permission denied")

    def _access(
        self, context: RunContext, operation: SessionOperation, *, allow_deleted: bool = False
    ) -> Session:
        session = self._session(context, allow_deleted=allow_deleted)
        self._authorize(context, operation, session)
        return session

    def _save_session(self, db: sqlite3.Connection, context: RunContext, session: Session) -> None:
        db.execute(
            "INSERT INTO sdk_sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET "
            "payload=excluded.payload",
            (self._key(context), session.model_dump_json()),
        )

    def _turn(self, context: RunContext, turn_id: str) -> SessionTurn:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM sdk_turns WHERE session_key=? AND id=?",
                (self._key(context), turn_id),
            ).fetchone()
        self.counters.turn_queries += 1
        if row is None:
            raise SessionUnavailable("turn unavailable")
        turn = SessionTurn.model_validate_json(row[0])
        if turn.session_id != context.session_id:
            raise SessionUnavailable("turn binding changed")
        return turn

    def _read_turns(
        self, context: RunContext, query: str, parameters: tuple[Any, ...]
    ) -> dict[str, SessionTurn]:
        """Run one batched turn-row query and return the rows it found (STATE-003)."""
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        self.counters.turn_queries += 1
        turns = {}
        for turn_id, payload in rows:
            turn = SessionTurn.model_validate_json(payload)
            if turn.session_id != context.session_id:
                raise SessionUnavailable("turn binding changed")
            turns[turn_id] = turn
        return turns

    def _turns(self, context: RunContext, turn_ids: tuple[str, ...]) -> dict[str, SessionTurn]:
        """Read several named turns of this session in one query (STATE-003)."""
        unique = sorted(set(turn_ids))
        if not unique:
            return {}
        return self._read_turns(
            context,
            "SELECT id, payload FROM sdk_turns WHERE session_key=? AND id IN "
            f"({_in_clause(len(unique))})",
            (self._key(context), *unique),
        )

    def _required_turns(
        self, turn_ids: tuple[str, ...], available: dict[str, SessionTurn]
    ) -> tuple[SessionTurn, ...]:
        """Return named turns in order; a reference without a row is refused, never skipped."""
        ordered = []
        for turn_id in turn_ids:
            turn = available.get(turn_id)
            if turn is None:
                raise SessionUnavailable("turn unavailable")
            ordered.append(turn)
        return tuple(ordered)

    def _update_turn(self, turn: SessionTurn) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE sdk_turns SET payload=? WHERE session_key=? AND id=?",
                (turn.model_dump_json(), self._key(turn.context), turn.id),
            )
            row = db.execute(
                "SELECT payload FROM sdk_sessions WHERE id=?", (self._key(turn.context),)
            ).fetchone()
            if row is None:
                raise SessionUnavailable("session disappeared")
            session = Session.model_validate_json(row[0])
            if session.active_turn == turn.id and turn.status not in _ACTIVE:
                self._save_session(
                    db,
                    turn.context,
                    session.model_copy(
                        update={"active_turn": None, "revision": session.revision + 1}
                    ),
                )

    def _record_sources(self, turn: SessionTurn, refs: tuple[SourceRef, ...]) -> None:
        """Persist the version-bound sources a governed tool read, de-duplicated."""
        if not refs:
            return
        latest = self._turn(turn.context, turn.id)
        unique = list(latest.sources)
        for ref in refs:
            if ref not in unique:
                unique.append(ref)
        self._update_turn(latest.model_copy(update={"sources": tuple(unique)}))

    def _validate_sources(self, context: RunContext, refs: tuple[SourceRef, ...]) -> None:
        """Re-check every governed source before stored output may be served again."""
        if not refs:
            return
        for ref in refs:
            if (
                ref.resource_id not in context.scope.resources
                and f"case:{ref.resource_id}" not in context.scope.resources
            ):
                # A resource outside the caller's current grant is not readable now,
                # whatever it was when the turn ran.
                self.counters.source_checks += 1
                raise SessionUnavailable("source outside current scope")
        self.counters.source_checks += 1
        if self.source_validator is None or self.source_validator(context, refs) is not True:
            raise SessionUnavailable("source version unavailable or current permission denied")

    def _record_materials(self, turn: SessionTurn, materials: tuple[ContextMaterial, ...]) -> None:
        latest = self._turn(turn.context, turn.id)
        unique = {(item.id, item.version, item.resource_id): item for item in latest.materials}
        unique.update({(item.id, item.version, item.resource_id): item for item in materials})
        self._update_turn(latest.model_copy(update={"materials": tuple(unique.values())}))

    def _validate_materials(
        self, context: RunContext, materials: tuple[ContextMaterial, ...]
    ) -> None:
        for material in materials:
            if (
                material.application_id != context.application_id
                or material.tenant_id != context.scope.tenant_id
                or (
                    material.resource_id is not None
                    and material.resource_id not in context.scope.resources
                )
                or (material.expires_at is not None and material.expires_at <= datetime.now(UTC))
            ):
                raise SessionUnavailable("source outside current scope or expired")
        if materials and (
            self.material_validator is None
            or self.material_validator(context, materials) is not True
        ):
            raise SessionUnavailable("source version unavailable or current permission denied")

    def _execution_application(
        self, original: ApplicationSpec, turn: SessionTurn, validation: _Validation | None
    ) -> ApplicationSpec:
        """Bind providers to this turn so its materials and history are recorded."""
        return replace(
            original,
            context_providers=(
                *(
                    _RecordingProvider(self, turn, provider)
                    for provider in original.context_providers
                ),
                _HistoryProvider(self, turn, validation),
            ),
        )

    def _workflow_spec(
        self, spec: WorkflowSpec, turn: SessionTurn, validation: _Validation | None
    ) -> WorkflowSpec:
        """Workflow definition whose node providers record into this turn."""
        return replace(
            spec,
            steps=tuple(
                replace(
                    step,
                    application=self._execution_application(step.application, turn, validation),
                )
                for step in spec.steps
            ),
        )

    def _controller(self, turn: SessionTurn, validation: _Validation | None = None) -> Controller:
        """Per-turn execution controller; providers are bound to this turn only."""
        directory = self.state_dir / "executions" / self._key(turn.context)
        if isinstance(self.application, ApplicationSpec):
            return Harness(
                self._execution_application(self.application, turn, validation), state_dir=directory
            )
        return WorkflowRunner(
            self._workflow_spec(self.application, turn, validation), state_dir=directory
        )

    def _lookup_controller(self, turn: SessionTurn) -> Controller:
        """Controller for evidence reads and cleanup, reused per session.

        Providers are only consumed by run/resume, so one Harness instance can serve
        every turn of a session instead of one construction per probe (STATE-003).
        Workflow reads keep a per-turn instance: a workflow definition digest covers
        the wrapper provider types, which are bound to one execution.
        """
        directory = self.state_dir / "executions" / self._key(turn.context)
        if isinstance(self.application, WorkflowSpec):
            return WorkflowRunner(
                self._workflow_spec(self.application, turn, None), state_dir=directory
            )
        controller = self._lookups.get(self._key(turn.context))
        if controller is None:
            self.counters.controller_lookups += 1
            controller = Harness(self.application, state_dir=directory)
            self._lookups[self._key(turn.context)] = controller
        return controller

    def _find(self, turn: SessionTurn) -> Result | None:
        controller = self._lookup_controller(turn)
        if isinstance(controller, Harness):
            return controller.find(turn.context)
        # WorkflowRunner deliberately has no nullable lookup. Never turn a denied
        # or tombstoned workflow into a new execution by guessing from an error.
        return controller.inspect(turn.context)

    def _evidence(self, turn: SessionTurn, state: _Validation) -> Result | None:
        """Probe this turn's run once per operation (STATE-003)."""
        if turn.id not in state.runs:
            self.counters.run_lookups += 1
            state.runs[turn.id] = self._find(turn)
        return state.runs[turn.id]

    def _validate_turn(
        self, context: RunContext, turn: SessionTurn, state: _Validation | None = None
    ) -> None:
        """Validate a turn and every ancestor, iteratively and once per operation.

        Validation is deliberately not truncated below the work bound: an ancestor
        whose material was withdrawn or whose evidence disappeared still refuses the
        read, however far back it sits (STATE-003).
        """
        state = _Validation() if state is None else state
        state.loaded.setdefault(turn.id, turn)
        state.anchor = max(state.anchor, turn.sequence)
        pending = [turn]
        while pending:
            current = pending.pop()
            if state.done.get(current.id) == current:
                continue
            state.done[current.id] = current
            self.counters.turns_validated += 1
            if len(state.done) > _MAX_VALIDATED_TURNS:
                # Refusing beats validating part of the chain: a truncated check
                # would accept history it never verified.
                raise SessionUnavailable(
                    "session history exceeds the validation bound; start a new session"
                )
            if current.status == "deleted":
                raise SessionUnavailable("turn evidence deleted")
            self._validate_materials(context, current.materials)
            self._validate_sources(context, current.sources)
            result = self._evidence(current, state)
            if current.status == RunStatus.SUCCEEDED and result is None:
                raise SessionUnavailable("successful turn evidence missing")
            if result is not None:
                evidence = (
                    result.tool_evidence
                    if isinstance(result, RunResult)
                    else tuple(
                        item for node in result.nodes.values() for item in node.tool_evidence
                    )
                )
                try:
                    refs = governed_source_refs(evidence, self.governed_tools)
                except ValueError as exc:
                    raise SessionUnavailable("durable source evidence is invalid") from exc
                self._validate_sources(context, refs)
            pending.extend(self._ancestors(current, state))

    def _ancestors(self, turn: SessionTurn, state: _Validation) -> tuple[SessionTurn, ...]:
        """Load this turn's ancestors with batched reads only (STATE-003)."""
        wanted = {turn_id for turn_id in turn.history_turns if turn_id not in state.loaded}
        if wanted:
            self._load_ancestors(turn, state, wanted)
        return self._required_turns(turn.history_turns, state.loaded)

    def _load_ancestors(self, turn: SessionTurn, state: _Validation, wanted: set[str]) -> None:
        """One window query per operation, then one batch for references outside it."""
        key = self._key(turn.context)
        if not state.windowed:
            # The anchor is the newest turn of this operation, so one query reads
            # the whole reachable chain instead of one query per ancestor.
            state.windowed = True
            state.loaded.update(
                self._read_turns(
                    turn.context,
                    "SELECT id, payload FROM sdk_turns WHERE session_key=? AND sequence<=? "
                    "ORDER BY sequence DESC LIMIT ?",
                    (key, state.anchor, _MAX_VALIDATED_TURNS),
                )
            )
        missing = sorted(wanted - set(state.loaded))
        if missing:
            # References outside the window (beyond the bound, or not ordered by
            # sequence) are still fetched and validated, never skipped.
            state.loaded.update(
                self._read_turns(
                    turn.context,
                    "SELECT id, payload FROM sdk_turns WHERE session_key=? AND id IN "
                    f"({_in_clause(len(missing))})",
                    (key, *missing),
                )
            )

    def _current(
        self, context: RunContext, turn: SessionTurn, state: _Validation | None = None
    ) -> None:
        if context.actor != turn.context.actor:
            raise SessionUnavailable("owner roles changed; start a new execution")
        self._validate_turn(context, turn, state)

    def create(self, context: RunContext) -> Session:
        self._authorize(context, SessionOperation.CREATE, None)
        with self._operation(context), self._connect() as db:
            existing = db.execute(
                "SELECT payload FROM sdk_sessions WHERE id=?", (self._key(context),)
            ).fetchone()
            if existing is not None:
                if Session.model_validate_json(existing[0]).status == "deleted":
                    # Tombstones are permanent: deleting a conversation retires its
                    # identity instead of freeing it for a different history.
                    raise SessionUnavailable("deleted session identity cannot be reused")
                raise SessionConflict("session identity already exists")
            session = Session(
                id=context.session_id,
                application_id=context.application_id,
                application_version=context.application_version,
                policy_version=context.policy_version,
                tenant_id=context.scope.tenant_id,
                actor_id=context.actor.id,
                resources=context.scope.resources,
                scope=context.scope,
            )
            self._save_session(db, context, session)
        return session

    def get(self, context: RunContext) -> Session:
        return self._access(context, SessionOperation.READ)

    def history(
        self, context: RunContext, *, limit: int = 20, before_sequence: int | None = None
    ) -> tuple[SessionTurn, ...]:
        self._access(context, SessionOperation.READ)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("history limit must be between 1 and 100")
        if before_sequence is not None and (
            type(before_sequence) is not int or before_sequence < 1
        ):
            raise ValueError("before_sequence must be positive")
        turns = self._history(context, limit, before_sequence)
        state = _Validation()
        for turn in turns:
            if turn.status != "deleted":
                self._validate_turn(context, turn, state)
        return turns

    def _history(
        self, context: RunContext, limit: int, before_sequence: int | None = None
    ) -> tuple[SessionTurn, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM sdk_turns WHERE session_key=? AND (? IS NULL OR sequence<?) "
                "ORDER BY sequence DESC LIMIT ?",
                (self._key(context), before_sequence, before_sequence, limit),
            ).fetchall()
        return tuple(SessionTurn.model_validate_json(row[0]) for row in reversed(rows))

    def get_turn(self, context: RunContext, turn_id: str) -> SessionTurn:
        self._access(context, SessionOperation.READ)
        turn = self._turn(context, turn_id)
        self._validate_turn(context, turn)
        return turn

    async def submit(self, context: RunContext, *, request_id: str, text: str) -> SessionTurn:
        self._access(context, SessionOperation.SUBMIT)
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > 200
            or self.transform.transform(request_id) != request_id
        ):
            raise ValueError("request_id must be a bounded credential-free identity")
        if not isinstance(text, str) or not text.strip() or len(text) > self.max_message_chars:
            raise ValueError("message is empty or exceeds limit")
        clean = str(self.transform.transform(text.strip()))
        request_digest = _digest([clean, context.actor.id])
        with self._operation(context):
            session = self._access(context, SessionOperation.SUBMIT)
            with self._connect() as db:
                row = db.execute(
                    "SELECT digest,payload FROM sdk_turns WHERE session_key=? AND request_id=?",
                    (self._key(context), request_id),
                ).fetchone()
            if row is not None:
                if row[0] != request_digest:
                    raise SessionConflict("request identity reused with different text")
                turn = SessionTurn.model_validate_json(row[1])
                self._validate_turn(context, turn)
                return self._reconcile(turn)
            if session.status != "open":
                raise SessionConflict("session is closed")
            if session.active_turn is not None:
                active = self._turn(context, session.active_turn)
                self._reconcile(active)
                session = self._session(context)
                if session.active_turn is not None:
                    raise SessionConflict("finish or delete the active turn before submitting")
            history = tuple(
                item
                for item in self._history(context, self.history_limit)
                if item.status == RunStatus.SUCCEEDED
            )
            state = _Validation()
            for previous in history:
                self._validate_turn(context, previous, state)
            turn_id = _digest([self._key(context), request_id])
            binding = context.model_copy(update={"run_id": f"turn-{turn_id}"})
            with self._connect() as db:
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM sdk_turns WHERE session_key=?",
                    (self._key(context),),
                ).fetchone()[0]
                turn = SessionTurn(
                    id=turn_id,
                    session_id=session.id,
                    sequence=sequence,
                    request_id=request_id,
                    run_id=binding.run_id,
                    workflow_id=binding.run_id
                    if isinstance(self.application, WorkflowSpec)
                    else None,
                    context=binding,
                    text=clean,
                    history_turns=tuple(item.id for item in history),
                )
                db.execute(
                    "INSERT INTO sdk_turns VALUES(?,?,?,?,?,?)",
                    (
                        self._key(context),
                        turn.id,
                        sequence,
                        request_id,
                        request_digest,
                        turn.model_dump_json(),
                    ),
                )
                self._save_session(
                    db,
                    context,
                    session.model_copy(
                        update={"active_turn": turn.id, "revision": session.revision + 1}
                    ),
                )
            return await self._execute(context, turn, start=True, validation=state)

    def _result(self, turn: SessionTurn, result: Result) -> SessionTurn:
        latest = self._turn(turn.context, turn.id)
        if isinstance(result, RunResult):
            output = result.final_output
        else:
            # A DAG may have several sinks. Expose a deterministic keyed result,
            # never pretend an arbitrary final node is the entire workflow answer.
            output = (
                json.dumps(
                    {name: node.final_output for name, node in sorted(result.nodes.items())},
                    ensure_ascii=False,
                )
                if result.status == RunStatus.SUCCEEDED
                else None
            )
        evidence = (
            result.tool_evidence
            if isinstance(result, RunResult)
            else tuple(item for node in result.nodes.values() for item in node.tool_evidence)
        )
        refs = governed_source_refs(evidence, self.governed_tools)
        updated = latest.model_copy(
            update={
                "status": result.status,
                "output": self.transform.transform(output),
                "error": self.transform.transform(result.error),
                "sources": tuple(dict.fromkeys((*latest.sources, *refs))),
            }
        )
        self._update_turn(updated)
        return updated

    def _reconcile(self, turn: SessionTurn) -> SessionTurn:
        if turn.status not in _ACTIVE:
            return turn
        result = self._find(turn)
        return turn if result is None else self._result(turn, result)

    async def _execute(
        self,
        context: RunContext,
        turn: SessionTurn,
        *,
        start: bool = False,
        grant: ApprovalGrant | None = None,
        approver: RunContext | None = None,
        validation: _Validation | None = None,
    ) -> SessionTurn:
        controller = self._controller(turn, validation)
        try:
            if start:
                result = await controller.run(context=turn.context, task=turn.text)
            else:
                previous = self._find(turn)
                if previous is None:
                    if turn.status != "starting" or grant is not None:
                        raise SessionUnavailable("execution evidence missing")
                    result = await controller.run(context=turn.context, task=turn.text)
                elif previous.status not in {RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL}:
                    if grant is not None:
                        # Grants are single-use. Returning the stored result would
                        # acknowledge a replayed grant as if it had been consumed now.
                        raise SessionConflict("approval grant no longer applies to a finished turn")
                    result = previous
                else:
                    result = await controller.resume(
                        context=turn.context, grant=grant, approver=approver
                    )
        except Exception:
            # An ordinary preparation error can precede a checkpoint. Preserve durable
            # state when present, never infer that a side effect happened, and never
            # leave the turn looking active when nothing was started.
            if isinstance(controller, Harness):
                previous = controller.find(turn.context)
            else:
                try:
                    previous = controller.inspect(turn.context)
                except ScopeError:
                    # A workflow with no durable state never started a node; a denied
                    # or tombstoned one has none either. Recording a failure claims
                    # only that this attempt produced nothing durable.
                    previous = None
            if previous is not None:
                self._result(turn, previous)
            else:
                latest = self._turn(turn.context, turn.id)
                self._update_turn(
                    latest.model_copy(
                        update={
                            "status": RunStatus.FAILED,
                            "error": "execution_preparation_failed",
                        }
                    )
                )
            raise
        updated = self._result(turn, result)
        self._access(context, SessionOperation.READ)
        self._validate_turn(context, updated)
        return updated

    def inspect(self, context: RunContext, turn_id: str) -> Result:
        self._access(context, SessionOperation.INSPECT)
        turn = self._turn(context, turn_id)
        self._validate_turn(context, turn)
        result = self._find(turn)
        if result is None:
            raise SessionUnavailable("execution has not started")
        return result

    def events(
        self, context: RunContext, turn_id: str, *, after: int = 0, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        self._access(context, SessionOperation.EVENTS)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid event pagination")
        turn = self._turn(context, turn_id)
        self._validate_turn(context, turn)
        controller = self._lookup_controller(turn)
        if isinstance(controller, WorkflowRunner):
            return tuple(
                {"sequence": index, **event}
                for index, event in enumerate(controller.events(turn.context), start=1)
                if after < index <= after + limit
            )
        path = controller.trace_path(turn.context)
        if not verify_trace(path).valid:
            raise SessionUnavailable("execution trace integrity check failed")
        envelopes = [
            TraceEnvelope.model_validate_json(line) for line in path.read_text().splitlines()
        ]
        return tuple(
            item.event.model_dump(mode="json")
            for item in envelopes
            if after < item.event.sequence <= after + limit
        )

    def approve(self, context: RunContext, turn_id: str, *, approver: RunContext) -> ApprovalGrant:
        with self._operation(context):
            session = self._access(context, SessionOperation.APPROVE)
            self._authorize(approver, SessionOperation.APPROVE, session, delegated=True)
            turn = self._turn(context, turn_id)
            state = _Validation()
            self._current(context, turn, state)
            if session.status != "open":
                raise SessionConflict("closed session cannot approve execution")
            return self._controller(turn).approve(context=turn.context, approver=approver)

    async def resume(
        self,
        context: RunContext,
        turn_id: str,
        *,
        grant: ApprovalGrant | None = None,
        approver: RunContext | None = None,
    ) -> SessionTurn:
        with self._operation(context):
            session = self._access(context, SessionOperation.RESUME)
            turn = self._turn(context, turn_id)
            state = _Validation()
            self._current(context, turn, state)
            if session.status != "open":
                raise SessionConflict("closed session cannot resume execution")
            if grant is not None:
                if approver is None:
                    raise SessionUnavailable("fresh approver identity is required")
                self._authorize(approver, SessionOperation.APPROVE, session, delegated=True)
            elif approver is not None:
                raise ValueError("approver requires an approval grant")
            return await self._execute(
                context, turn, grant=grant, approver=approver, validation=state
            )

    def close(self, context: RunContext) -> Session:
        with self._operation(context):
            session = self._access(context, SessionOperation.CLOSE)
            if session.active_turn is not None:
                self._reconcile(self._turn(context, session.active_turn))
                session = self._session(context)
                if session.active_turn is not None:
                    raise SessionConflict("active turn prevents closing session")
            updated = session.model_copy(
                update={"status": "closed", "revision": session.revision + 1}
            )
            with self._connect() as db:
                self._save_session(db, context, updated)
            return updated

    def _delete_turn(self, turn: SessionTurn) -> None:
        """Tombstone the turn, then finish controller cleanup re-entrantly.

        The turn row is the cleanup progress marker: it is tombstoned before any
        controller call and only dropped by session deletion, so a retry after a
        crash resumes exactly the same turn.
        """
        self._update_turn(
            turn.model_copy(
                update={
                    "status": "deleted",
                    "text": "",
                    "output": None,
                    "error": None,
                    "materials": (),
                    "sources": (),
                    "history_turns": (),
                }
            )
        )
        controller = self._lookup_controller(turn)
        if isinstance(controller, Harness):
            # A preparation failure records a failed turn before the first
            # checkpoint exists, and such a turn must still be deletable (STATE-002).
            # `allow_missing` tolerates only genuine absence for this exact binding:
            # a checkpoint or tombstone owned by another execution binding keeps
            # raising, so a denial is never reported as a successful delete.
            controller.delete(turn.context, allow_missing=True)
        else:
            controller.delete(turn.context, allow_missing=True)

    def delete_turn(self, context: RunContext, turn_id: str) -> None:
        with self._operation(context):
            self._access(context, SessionOperation.DELETE)
            self._delete_turn(self._turn(context, turn_id))

    def delete(self, context: RunContext) -> None:
        """Delete every turn, continuing past one that cannot be cleaned.

        A refused turn keeps its tombstoned row so a later retry can finish it and
        the surface error is never mistaken for a completed deletion.
        """
        with self._operation(context):
            session = self._access(context, SessionOperation.DELETE, allow_deleted=True)
            key = self._key(context)
            with self._connect() as db:
                self._save_session(db, context, session.model_copy(update={"status": "deleted"}))
                rows = db.execute(
                    "SELECT payload FROM sdk_turns WHERE session_key=? ORDER BY sequence",
                    (key,),
                ).fetchall()
            failures: list[Exception] = []
            for row in rows:
                turn = SessionTurn.model_validate_json(row[0])
                try:
                    self._delete_turn(turn)
                except Exception as error:
                    failures.append(error)
                    continue
                with self._connect() as db:
                    db.execute("PRAGMA secure_delete=ON")
                    db.execute("DELETE FROM sdk_turns WHERE session_key=? AND id=?", (key, turn.id))
            with closing(connect_wal(self.database)) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("VACUUM")
            if failures:
                raise failures[0]

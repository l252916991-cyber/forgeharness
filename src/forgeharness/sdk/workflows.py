"""Durable, sequential DAG composition over the public Harness controller.

Nodes reserve their full budgets up front. Each node is an ordinary SDK run;
its own dispatcher, current policy, approval and journal remain authoritative.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field

from forgeharness.domain.models import FrozenModel, RunContext, RunResult, RunStatus, Usage
from forgeharness.runtime.verification import VerificationResult
from forgeharness.sdk.application import ApplicationSpec, Harness, ScopeError, binding_digest
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform
from forgeharness.state.approval import ApprovalGrant
from forgeharness.state.checkpoint import CheckpointConflict
from forgeharness.state.sqlite import connect_wal


class WorkflowBudget(FrozenModel):
    """Static sum of per-node reservations, not a replenished per-resume budget."""

    max_steps: int = Field(default=100, ge=1)
    max_tool_calls: int = Field(default=200, ge=0)
    max_input_tokens: int = Field(default=1_000_000, ge=0)
    max_output_tokens: int = Field(default=200_000, ge=0)
    max_input_chars: int = Field(default=100_000, ge=1, le=1_000_000)


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    application: ApplicationSpec
    task: str
    depends_on: tuple[str, ...] = ()


class WorkflowVerificationRequest(FrozenModel):
    context: RunContext
    nodes: dict[str, RunResult]


class WorkflowVerifier(Protocol):
    async def verify(self, request: WorkflowVerificationRequest) -> VerificationResult: ...


@dataclass(frozen=True)
class WorkflowSpec:
    id: str
    version: str
    policy_version: str
    steps: tuple[WorkflowStep, ...]
    budget: WorkflowBudget = field(default_factory=WorkflowBudget)
    evidence_transform: EvidenceTransform = field(default_factory=CredentialMask)
    verifier: WorkflowVerifier | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.steps) <= 64:
            raise ValueError("workflow requires 1 to 64 steps")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.id):
            raise ValueError("invalid workflow identity")
        if not self.version or not self.policy_version:
            raise ValueError("workflow versions required")
        names = {step.id for step in self.steps}
        if len(names) != len(self.steps):
            raise ValueError("duplicate workflow step")
        for step in self.steps:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", step.id) or not step.task:
                raise ValueError("invalid step identity or task")
            if (
                len(set(step.depends_on)) != len(step.depends_on)
                or not set(step.depends_on) <= names
            ):
                raise ValueError("duplicate or unknown dependency")
            if (step.application.id, step.application.version, step.application.policy_version) != (
                self.id,
                self.version,
                self.policy_version,
            ):
                raise ValueError("steps must share the workflow application and policy versions")
        self.ordered_steps()
        for metric in ("steps", "tool_calls", "input_tokens", "output_tokens"):
            maximum = f"max_{metric}"
            if sum(getattr(step.application.budget, maximum) for step in self.steps) > getattr(
                self.budget, maximum
            ):
                raise ValueError(f"workflow {metric} reservations exceed budget")

    def ordered_steps(self) -> tuple[WorkflowStep, ...]:
        pending = {step.id: step for step in self.steps}
        ordered: list[WorkflowStep] = []
        completed: set[str] = set()
        while pending:
            ready = sorted(
                name for name, step in pending.items() if set(step.depends_on) <= completed
            )
            if not ready:
                raise ValueError("cyclic workflow dependencies")
            for name in ready:
                ordered.append(pending.pop(name))
                completed.add(name)
        return tuple(ordered)

    def digest(self) -> str:
        definition = {
            "id": self.id,
            "version": self.version,
            "policy_version": self.policy_version,
            "budget": self.budget.model_dump(),
            "verifier": type(self.verifier).__qualname__,
            "steps": [
                {
                    "id": step.id,
                    "task": step.task,
                    "depends_on": sorted(step.depends_on),
                    "budget": step.application.budget.model_dump(),
                    "instructions": step.application.instructions,
                    "policy": type(step.application.policy).__qualname__,
                    "verifier": type(step.application.verifier).__qualname__,
                    "providers": [type(p).__qualname__ for p in step.application.context_providers],
                    "tools": [tool.spec.model_dump(mode="json") for tool in step.application.tools],
                }
                for step in self.ordered_steps()
            ],
        }
        return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


class _State(FrozenModel):
    context: RunContext
    definition: str
    task: str
    status: RunStatus = RunStatus.RUNNING
    started: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    active: str | None = None
    error: str | None = None
    revision: int = 0
    deleted: bool = False
    verification: VerificationResult | None = None
    schema_version: Literal[1] = 1


class WorkflowResult(FrozenModel):
    run_id: str
    status: RunStatus
    active_node: str | None
    completed_nodes: tuple[str, ...]
    nodes: dict[str, RunResult]
    usage: Usage
    error: str | None = None
    verification: VerificationResult | None = None


@dataclass(frozen=True)
class _Transforms:
    outer: EvidenceTransform
    inner: EvidenceTransform

    def transform(self, value: Any) -> Any:
        return self.outer.transform(self.inner.transform(value))


class WorkflowRunner:
    """Single-machine coordinator; host reauthenticates before every public call."""

    def __init__(self, spec: WorkflowSpec, *, state_dir: Path) -> None:
        self.spec = spec
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.state_dir.stat().st_mode) & 0o077:
            raise ScopeError("workflow directory must be owner-private (mode 0700)")
        self.database = self.state_dir / "workflows.sqlite"
        self.steps = {step.id: step for step in spec.steps}
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, payload TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_events (sequence INTEGER PRIMARY KEY, "
                "id TEXT, payload TEXT)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(connect_wal(self.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _key(self, context: RunContext) -> str:
        RunContext.model_validate(context.model_dump())
        if (context.application_id, context.application_version, context.policy_version) != (
            self.spec.id,
            self.spec.version,
            self.spec.policy_version,
        ) or self.spec.evidence_transform.transform(
            context.model_dump(mode="json")
        ) != context.model_dump(mode="json"):
            raise ScopeError("invalid workflow binding or credentials in trusted identity")
        return hashlib.sha256(
            json.dumps([context.application_id, context.scope.tenant_id, context.run_id]).encode()
        ).hexdigest()

    @contextmanager
    def _operation(self, context: RunContext) -> Iterator[None]:
        with (self.state_dir / f"{self._key(context)}.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ScopeError("workflow has an active worker") from exc
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def _load(self, context: RunContext, *, allow_deleted: bool = False) -> _State:
        key = self._key(context)
        with self._connect() as db:
            row = db.execute("SELECT payload FROM workflows WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ScopeError("workflow unavailable for this binding")
        state = _State.model_validate_json(row[0])
        if binding_digest(context) != binding_digest(state.context):
            raise ScopeError("workflow unavailable for this binding")
        if state.deleted and not allow_deleted:
            raise ScopeError("workflow deleted")
        if state.definition != self.spec.digest():
            raise CheckpointConflict(
                "workflow definition changed; resume requires the original spec"
            )
        return state

    def _save(self, state: _State, event: str, **changes: Any) -> _State:
        if changes.get("error") is not None:
            changes["error"] = str(self.spec.evidence_transform.transform(changes["error"]))
        updated = state.model_copy(update={**changes, "revision": state.revision + 1})
        key = self._key(state.context)
        with self._connect() as db:
            current = db.execute("SELECT payload FROM workflows WHERE id=?", (key,)).fetchone()
            if current is not None:
                old = _State.model_validate_json(current[0])
                if old.revision != state.revision or old.context != state.context:
                    raise CheckpointConflict("workflow state changed")
            elif state.revision != 0:
                raise CheckpointConflict("workflow state disappeared")
            db.execute(
                "INSERT INTO workflows VALUES(?,?) ON CONFLICT(id) "
                "DO UPDATE SET payload=excluded.payload",
                (key, updated.model_dump_json()),
            )
            db.execute(
                "INSERT INTO workflow_events(id,payload) VALUES(?,?)",
                (
                    key,
                    json.dumps(
                        {
                            "event": event,
                            "node": state.active if event == "node.completed" else updated.active,
                            "at": datetime.now(UTC).isoformat(),
                            "revision": updated.revision,
                        }
                    ),
                ),
            )
        return updated

    def _child(self, context: RunContext, step: WorkflowStep) -> tuple[Harness, RunContext]:
        key = self._key(context)
        child_context = context.model_copy(update={"run_id": f"wf-{key}.{step.id}"})
        # Keep IDs within the public TaskId contract even at maximum node-name length.
        child_context = RunContext.model_validate(
            child_context.model_copy(
                update={"run_id": hashlib.sha256(child_context.run_id.encode()).hexdigest()}
            ).model_dump()
        )
        application = replace(
            step.application,
            evidence_transform=_Transforms(
                self.spec.evidence_transform, step.application.evidence_transform
            ),
        )
        harness = Harness(application, state_dir=self.state_dir / "children" / key / step.id)
        return harness, child_context

    def inspect(self, context: RunContext) -> WorkflowResult:
        state = self._load(context)
        nodes: dict[str, RunResult] = {}
        for name in state.started:
            harness, child = self._child(context, self.steps[name])
            result = harness.find(child)
            if result is not None:
                nodes[name] = result
        usage = Usage(
            **{
                metric: sum(getattr(result.usage, metric) for result in nodes.values())
                for metric in Usage.model_fields
            }
        )
        return WorkflowResult(
            run_id=context.run_id,
            status=state.status,
            active_node=state.active,
            completed_nodes=state.completed,
            nodes=nodes,
            usage=usage,
            error=state.error,
            verification=state.verification,
        )

    def events(self, context: RunContext) -> tuple[dict[str, Any], ...]:
        self._load(context)
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM workflow_events WHERE id=? ORDER BY sequence",
                (self._key(context),),
            ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    async def run(self, *, context: RunContext, task: str) -> WorkflowResult:
        with self._operation(context):
            with self._connect() as db:
                if db.execute(
                    "SELECT 1 FROM workflows WHERE id=?", (self._key(context),)
                ).fetchone():
                    raise CheckpointConflict("workflow already exists; inspect or resume")
            clean = str(self.spec.evidence_transform.transform(task))
            if not clean or len(clean) > self.spec.budget.max_input_chars:
                raise ValueError("workflow task empty or exceeds input limit")
            state = self._save(
                _State(context=context, definition=self.spec.digest(), task=clean), "started"
            )
            return await self._execute(state)

    def _task(self, state: _State, step: WorkflowStep) -> str:
        dependencies = {}
        for name in step.depends_on:
            harness, child = self._child(state.context, self.steps[name])
            result = harness.inspect(child)
            if result.status != RunStatus.SUCCEEDED:
                raise CheckpointConflict("dependency is not complete")
            dependencies[name] = {
                "output": result.final_output,
                "evidence": [e.model_dump(mode="json") for e in result.tool_evidence],
            }
        task = json.dumps(
            {"task": state.task, "step": step.task, "untrusted_dependency_data": dependencies},
            ensure_ascii=False,
        )
        if len(task) > self.spec.budget.max_input_chars:
            raise ValueError("workflow dependency input exceeds limit")
        return task

    async def _execute(
        self, state: _State, grant: ApprovalGrant | None = None, approver: RunContext | None = None
    ) -> WorkflowResult:
        for step in self.spec.ordered_steps():
            if step.id in state.completed:
                continue
            if step.id not in state.started:
                state = self._save(
                    state,
                    "node.started",
                    active=step.id,
                    started=(*state.started, step.id),
                    status=RunStatus.RUNNING,
                )
            try:
                harness, child = self._child(state.context, step)
                previous = harness.find(child)
                if grant is not None and (
                    step.id != state.active
                    or previous is None
                    or previous.status != RunStatus.AWAITING_APPROVAL
                ):
                    raise ScopeError("approval does not address the current waiting node")
                if previous is None:
                    if grant is not None:
                        raise ScopeError("approval does not address a waiting node")
                    result = await harness.run(context=child, task=self._task(state, step))
                elif previous.status == RunStatus.AWAITING_APPROVAL and grant is not None:
                    if approver is None:
                        raise ScopeError("fresh approver context required")
                    self._key(approver)
                    result = await harness.resume(
                        context=child,
                        grant=grant,
                        approver=approver.model_copy(update={"run_id": child.run_id}),
                    )
                    grant = None
                elif previous.status == RunStatus.RUNNING:
                    result = await harness.resume(context=child)
                else:
                    result = previous
            except Exception as exc:
                # Keep exception content, which may contain case data or secrets, out of evidence.
                self._save(
                    state, "node.control_error", error=f"node_control_error:{type(exc).__name__}"
                )
                raise
            if result.status != RunStatus.SUCCEEDED:
                self._save(
                    state,
                    "node.suspended"
                    if result.status == RunStatus.AWAITING_APPROVAL
                    else "node.failed",
                    status=result.status,
                    error=result.error,
                )
                return self.inspect(state.context)
            state = self._save(
                state,
                "node.completed",
                completed=(*state.completed, step.id),
                active=None,
                status=RunStatus.RUNNING,
                error=None,
            )
        if self.spec.verifier is not None:
            try:
                verdict = await self.spec.verifier.verify(
                    WorkflowVerificationRequest(
                        context=state.context,
                        nodes=self.inspect(state.context).nodes,
                    )
                )
                verdict = VerificationResult.model_validate(
                    self.spec.evidence_transform.transform(verdict.model_dump(mode="json"))
                )
            except Exception as exc:
                verdict = VerificationResult(
                    passed=False, reason=f"workflow_verifier_error:{type(exc).__name__}"
                )
            state = self._save(state, "verification.completed", verification=verdict)
            if not verdict.passed:
                self._save(state, "failed", status=RunStatus.FAILED, error=verdict.reason)
                return self.inspect(state.context)
        self._save(state, "completed", status=RunStatus.SUCCEEDED, active=None)
        return self.inspect(state.context)

    def approve(self, *, context: RunContext, approver: RunContext) -> ApprovalGrant:
        with self._operation(context):
            state = self._load(context)
            if state.status != RunStatus.AWAITING_APPROVAL or state.active is None:
                raise ScopeError("workflow is not awaiting approval")
            self._key(approver)
            harness, child = self._child(context, self.steps[state.active])
            return harness.approve(
                context=child, approver=approver.model_copy(update={"run_id": child.run_id})
            )

    async def resume(
        self,
        *,
        context: RunContext,
        grant: ApprovalGrant | None = None,
        approver: RunContext | None = None,
    ) -> WorkflowResult:
        with self._operation(context):
            state = self._load(context)
            if state.status not in {RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL}:
                raise CheckpointConflict("only running or approval-suspended workflows can resume")
            if grant is not None and (
                state.status != RunStatus.AWAITING_APPROVAL or approver is None
            ):
                raise ScopeError("approval requires a waiting workflow and fresh approver")
            return await self._execute(state, grant, approver)

    def delete(self, context: RunContext, *, allow_missing: bool = False) -> None:
        """Tombstone first, then retryable child cleanup; domain records are separate."""
        with self._operation(context):
            # Check absence separately: a refusal from _load may be a foreign
            # binding or changed definition and must never become successful cleanup.
            with self._connect() as db:
                present = db.execute(
                    "SELECT 1 FROM workflows WHERE id=?", (self._key(context),)
                ).fetchone()
            if present is None:
                if not allow_missing:
                    raise ScopeError("workflow unavailable for this binding")
                state = _State(context=context, definition=self.spec.digest(), task="")
            else:
                state = self._load(context, allow_deleted=True)
            if not state.deleted:
                state = self._save(
                    state, "deleted", deleted=True, task="", error=None, verification=None
                )
            failures: list[Exception] = []
            for name in state.started:
                harness, child = self._child(context, self.steps[name])
                try:
                    harness.delete(child, allow_missing=True)
                except Exception as error:
                    failures.append(error)
            with self._connect() as db:
                db.execute("DELETE FROM workflow_events WHERE id=?", (self._key(context),))
            if failures:
                raise failures[0]

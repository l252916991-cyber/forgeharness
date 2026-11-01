"""Mutable todo state that survives compaction and gates the final answer.

A plan written before the first model call is a prediction. Work that runs for
dozens of steps contradicts predictions, and the runtime needs somewhere to record
the correction that is neither the conversation nor a frozen object. This module is
that place: a small, revisioned board the model edits through one tool.

Two properties make the board load-bearing rather than decorative:

* it is *pinned* into every request (see :mod:`forgeharness.context.assembly`), so
  compaction can never elide the objective it encodes;
* a run that still has an open item cannot finish. A verifier can prove that the
  final answer has evidence; only the board can prove the work matches the request.

The board is runtime state, not model text: it is written by a tool call, revisioned
on every change, and reconstructed from the trace rather than parsed out of prose.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import FrozenModel, Message, MessageRole, ToolCall, ToolEvidence
from forgeharness.tools.base import RiskLevel, ToolContext, ToolEffectClass, ToolOutput, ToolSpec

# A board longer than this stops being a plan and becomes a second transcript.
MAX_TODOS = 50
# Rendered every turn, so the text budget is a real per-request cost.
_MAX_CONTENT_CHARS = 500
_MAX_RENDERED_CHARS = 4_000
_TODO_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class TodoStatus(StrEnum):
    """The three states a work item can be in."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


_STATUS_MARKERS: dict[TodoStatus, str] = {
    TodoStatus.PENDING: " ",
    TodoStatus.IN_PROGRESS: "~",
    TodoStatus.COMPLETED: "x",
}


class TodoItem(FrozenModel):
    """One unit of intended work with an operator-meaningful identity."""

    id: str = Field(pattern=_TODO_ID_PATTERN)
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_CHARS)
    status: TodoStatus = TodoStatus.PENDING


class TodoSnapshot(FrozenModel):
    """An immutable copy of the board at one revision.

    Frozen so a snapshot can travel into a trace event, a checkpoint or a test
    assertion without the board mutating underneath it.
    """

    revision: int = Field(ge=0)
    todos: tuple[TodoItem, ...] = ()

    @property
    def open_items(self) -> tuple[TodoItem, ...]:
        """Items that are not completed, in board order."""
        return tuple(item for item in self.todos if item.status is not TodoStatus.COMPLETED)

    @property
    def open_count(self) -> int:
        """How many items still block a final answer."""
        return len(self.open_items)

    @property
    def completed_count(self) -> int:
        """How many items are done."""
        return sum(1 for item in self.todos if item.status is TodoStatus.COMPLETED)

    @property
    def digest(self) -> str:
        """Stable identity of this revision's content, for idempotency checks."""
        payload = "\n".join(
            f"{item.id}\t{item.status.value}\t{item.content}" for item in self.todos
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class TodoBoard:
    """The single mutable holder of todo state for one run.

    Deliberately not a model: two things in the runtime may read it (the tool that
    writes it and the assembler that pins it), and neither should be able to
    construct a competing one.
    """

    def __init__(self, todos: Sequence[TodoItem] = ()) -> None:
        self._todos: tuple[TodoItem, ...] = _validated(todos)
        self._revision = 0 if not self._todos else 1

    @property
    def revision(self) -> int:
        """Current revision; increments only when the content actually changes."""
        return self._revision

    @property
    def items(self) -> tuple[TodoItem, ...]:
        """Current items in board order."""
        return self._todos

    def snapshot(self) -> TodoSnapshot:
        """Return a detached copy of the current state."""
        return TodoSnapshot(revision=self._revision, todos=self._todos)

    def replace(self, todos: Sequence[TodoItem]) -> TodoSnapshot:
        """Replace the whole board, incrementing the revision on a real change.

        A write that repeats the current content is a no-op: that is what makes the
        tool honestly ``IDEMPOTENT`` and lets an at-least-once dispatch replay the
        same call without inventing a revision.
        """
        validated = _validated(todos)
        if validated != self._todos:
            self._todos = validated
            self._revision += 1
        return self.snapshot()

    def reset(self) -> None:
        """Clear the board so a new run cannot inherit another task's plan."""
        self._todos = ()
        self._revision = 0

    def restore(self, todos: Sequence[TodoItem], *, revision: int | None = None) -> TodoSnapshot:
        """Reinstall board content recovered from a transcript.

        A replay supplies the proven revision. Callers restoring only content keep
        the original initialization behavior; neither path invents a mutation.
        """
        validated = _validated(todos)
        if revision is not None and (
            type(revision) is not int or revision < 0 or (validated and revision == 0)
        ):
            raise ValueError("todo restore needs a valid nonnegative revision")
        self._todos = validated
        self._revision = (0 if not self._todos else 1) if revision is None else revision
        return self.snapshot()


def replay_board(
    messages: Sequence[Message], *, evidence: Sequence[ToolEvidence] | None = None
) -> TodoBoard:
    """Apply only completed, correlated writes in their original revision order.

    An assistant proposal is untrusted intent. A tool observation must confirm its
    canonical successful board and expected revision before it becomes state.
    Runtime resumes also require their structured successful output: it retains
    the full result when the model-visible observation was compressed. Conflicting
    success evidence fails closed rather than silently clearing an earlier plan.
    """
    board = TodoBoard()
    pending: dict[str, ToolCall] = {}
    seen: set[str] = set()
    applied: set[str] = set()
    facts: dict[str, ToolEvidence] = {}
    if evidence is not None:
        for fact in evidence:
            if fact.name != "write_todos" or fact.logical_call_id is None:
                continue
            prior = facts.get(fact.logical_call_id)
            if prior is not None and prior != fact:
                raise ValueError("todo replay has conflicting output evidence")
            facts[fact.logical_call_id] = fact
    for message in messages:
        if message.role is MessageRole.ASSISTANT:
            for call in message.tool_calls:
                if call.id in seen:
                    continue
                seen.add(call.id)
                if call.name == "write_todos":
                    pending[call.id] = call
            continue
        if message.role is not MessageRole.TOOL:
            continue
        proposed = pending.pop(message.tool_call_id or "", None)
        if proposed is None or message.tool_name != proposed.name:
            continue
        try:
            payload = WriteTodosInput.model_validate(proposed.arguments)
            todos = _validated(payload.todos)
        except ValueError:
            continue
        revision = board.revision + (todos != board.items)
        snapshot = TodoSnapshot(revision=revision, todos=todos)
        canonical = render_todos(snapshot)
        completed_fact = facts.get(proposed.id)
        if evidence is not None:
            if completed_fact is None:
                if _success_observation(message.content):
                    raise ValueError("todo replay is missing its committed output evidence")
                continue
            if not completed_fact.ok:
                continue
            expected = {
                "revision": snapshot.revision,
                "open_count": snapshot.open_count,
                "completed_count": snapshot.completed_count,
                "digest": snapshot.digest,
                "todos": [item.model_dump(mode="json") for item in snapshot.todos],
            }
            if completed_fact.content != canonical or any(
                type(completed_fact.metadata.get(key)) is not type(value)
                or completed_fact.metadata.get(key) != value
                for key, value in expected.items()
            ):
                raise ValueError("todo replay output does not match its proposal or revision")
            compressed = (
                f"[compressed observation: {len(canonical)} chars, "
                f"{len(canonical.splitlines())} lines"
            )
            if message.content != canonical and not (message.content or "").startswith(compressed):
                raise ValueError("todo replay observation does not match its successful output")
        elif message.content != canonical:
            if _success_observation(message.content):
                raise ValueError("todo replay observation does not match its proposal or revision")
            continue
        board.restore(todos, revision=revision)
        applied.add(proposed.id)
    if any(fact.ok and identifier not in applied for identifier, fact in facts.items()):
        raise ValueError("todo replay successful output has no matching observation")
    return board


def _success_observation(content: str | None) -> bool:
    return (content or "").startswith(("[todo state:", "[compressed observation:"))


def _validated(todos: Sequence[TodoItem]) -> tuple[TodoItem, ...]:
    """Enforce the board-wide invariants a per-item model cannot express."""
    if len(todos) > MAX_TODOS:
        raise ValueError(f"a todo board may hold at most {MAX_TODOS} items")
    identifiers = [item.id for item in todos]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("todo ids must be unique")
    if sum(1 for item in todos if item.status is TodoStatus.IN_PROGRESS) > 1:
        # One in-flight item is the point: a board with three "current" tasks
        # cannot tell the model which one its next step belongs to.
        raise ValueError("at most one todo may be in progress")
    return tuple(todos)


def render_todos(snapshot: TodoSnapshot) -> str:
    """Render the board as the compact block pinned into every request.

    Deterministic up to the revision counter, and bounded, because it is paid for
    on every single model call.
    """
    header = (
        f"[todo state: revision {snapshot.revision}; "
        f"{snapshot.open_count} open, {snapshot.completed_count} completed]"
    )
    lines = [header]
    for item in snapshot.todos:
        marker = _STATUS_MARKERS[item.status]
        lines.append(f"- [{marker}] {item.id}: {item.content} ({item.status.value})")
    if snapshot.open_count:
        lines.append("[finish is refused while any item is open]")
    rendered = "\n".join(lines)
    if len(rendered) <= _MAX_RENDERED_CHARS:
        return rendered
    # Truncation keeps the header and the open items; the tail of a long board is
    # the least informative part of it.
    kept = [header]
    used = len(header)
    for line in lines[1:]:
        if used + len(line) + 1 > _MAX_RENDERED_CHARS:
            kept.append("[board truncated]")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def todo_pinned_message(snapshot: TodoSnapshot) -> str | None:
    """Return the pinned block for a snapshot, or ``None`` when the board is empty.

    An empty board pins nothing: a run that never planned must not pay for a
    header announcing that it has no plan.
    """
    if not snapshot.todos:
        return None
    return render_todos(snapshot)


class WriteTodosInput(BaseModel):
    """Arguments that replace the whole board.

    Whole-board replacement rather than per-item patching keeps the contract small
    enough that a model gets it right, and makes the write naturally idempotent.
    """

    model_config = ConfigDict(extra="forbid")

    todos: tuple[TodoItem, ...] = Field(default=(), max_length=MAX_TODOS)
    rationale: str | None = Field(default=None, max_length=300)


class WriteTodosTool:
    """Replace the run's todo board and report the resulting revision."""

    input_model = WriteTodosInput

    def __init__(self, board: TodoBoard) -> None:
        self._board = board
        self.spec = ToolSpec(
            name="write_todos",
            description=(
                "Replace the run's todo list. Send the complete list every time, with each "
                "item's status. Keep at most one item in_progress. Finishing is refused "
                "while any item is pending or in progress."
            ),
            input_schema=WriteTodosInput.model_json_schema(),
            # Harness state only: no workspace or network effect, so it is safe to
            # call without an approval round-trip.
            risk=RiskLevel.READ,
            # Same payload twice must not invent a revision, which is what makes the
            # at-least-once dispatch path safe to replay.
            effect_class=ToolEffectClass.IDEMPOTENT,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Validate, apply, and render the new board."""
        del context
        values = WriteTodosInput.model_validate(arguments)
        try:
            snapshot = self._board.replace(values.todos)
        except ValueError as exc:
            # A malformed board is the model's mistake to fix, not a run-ending
            # fault, so it comes back as an observation.
            return ToolOutput(
                ok=False,
                content=f"todo board rejected: {exc}",
                metadata={"error_code": "invalid_todos"},
            )
        return ToolOutput(
            ok=True,
            content=render_todos(snapshot),
            metadata={
                "revision": snapshot.revision,
                "open_count": snapshot.open_count,
                "completed_count": snapshot.completed_count,
                "digest": snapshot.digest,
                "todos": [item.model_dump(mode="json") for item in snapshot.todos],
                "rationale": values.rationale,
            },
        )


__all__ = [
    "MAX_TODOS",
    "TodoBoard",
    "TodoItem",
    "TodoSnapshot",
    "TodoStatus",
    "WriteTodosInput",
    "WriteTodosTool",
    "render_todos",
    "replay_board",
    "todo_pinned_message",
]

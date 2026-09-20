"""Tool Invocation Journal: crash-window safety (ADR 009).

The load-bearing property is not "the journal exists" but:

* a completed side effect is never executed a second time, even when the
  checkpoint that would have recorded it was lost; and
* when it cannot be confirmed whether a side effect happened, the runtime refuses
  to replay a non-idempotent call rather than guessing.

Each test kills the run at one fault-injection point and resumes it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from forgeharness.domain.models import (
    CURRENT_RECOVERY_SEMANTICS_VERSION,
    FinalAction,
    ModelResult,
    ModelUsage,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.state.invocation_journal import (
    InvocationState,
    JournalError,
    RecoveryDecision,
    SQLiteInvocationJournal,
    decide_recovery,
    terminal_state_for_failure,
)
from forgeharness.tools.base import (
    RiskLevel,
    Tool,
    ToolContext,
    ToolEffectClass,
    ToolErrorCode,
    ToolOutput,
    ToolSpec,
)
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.registry import ToolRegistry


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "x"


class CountingTool:
    """A tool with a real side effect, counted so a repeat is detectable."""

    def __init__(self, path: Path, effect_class: ToolEffectClass, name: str = "effect") -> None:
        self._path = path
        self._name = name
        self.input_model = _Input
        self.spec = ToolSpec(
            name=name,
            description="Append one line to a file.",
            input_schema=_Input.model_json_schema(),
            risk=RiskLevel.WRITE,
            effect_class=effect_class,
        )

    @property
    def executions(self) -> int:
        """How many times the side effect actually ran."""
        return self._path.read_text().count("\n") if self._path.exists() else 0

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        # The real side effect: appending a line is observable and duplicable.
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write("ran\n")
        return ToolOutput(ok=True, content="appended one line")


class CrashingTool(CountingTool):
    """Performs its side effect, then crashes before it can return."""

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del context
        del arguments
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write("ran\n")
        raise RuntimeError("crashed after the side effect")


class _AllowAll:
    def evaluate(self, *, task_id: str, spec: object, call: object) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test")


class _Scripted:
    """Return queued actions in order, then finish.

    It deliberately does not repeat the last action: a provider's tool-call ids are
    unique per call, so replaying one would manufacture a duplicate logical call and
    test the journal against a state the runtime cannot actually reach.
    """

    def __init__(self, actions: list[object]) -> None:
        self._actions = list(actions)
        self.calls = 0

    async def decide(self, request: object) -> ModelResult:
        del request
        self.calls += 1
        action = self._actions.pop(0) if self._actions else FinalAction(content="done")
        return ModelResult(action=action, usage=ModelUsage(input_tokens=5, output_tokens=2))


def _build(
    tmp_path: Path,
    tool: Tool,
    *,
    journal: SQLiteInvocationJournal,
    checkpoints: SQLiteCheckpointStore,
    actions: list[object],
) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentRuntime(
        model=_Scripted(actions),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
        checkpoint_store=checkpoints,
        invocation_journal=journal,
    )


def _stores(tmp_path: Path) -> tuple[SQLiteInvocationJournal, SQLiteCheckpointStore]:
    db = tmp_path / "runs.sqlite3"
    return SQLiteInvocationJournal(db), SQLiteCheckpointStore(db)


async def test_claimed_is_written_before_the_tool_runs(tmp_path: Path) -> None:
    """The journal must never claim in hindsight.

    `started` is committed before dispatch, so a crash inside the tool is
    discoverable. Asserting the ordering directly is what protects it.
    """
    journal, checkpoints = _stores(tmp_path)
    side_effect = tmp_path / "effects.txt"
    seen_states: list[InvocationState] = []

    class ObservingTool(CountingTool):
        async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
            # When the tool body starts, the journal must already say `started`.
            pending = journal.pending_for_run("task")
            seen_states.extend(record.state for record in pending)
            return await super().execute(arguments, context)

    tool = ObservingTool(side_effect, ToolEffectClass.READ_ONLY)
    runtime = _build(
        tmp_path,
        tool,
        journal=journal,
        checkpoints=checkpoints,
        actions=[
            ToolAction(call=ToolCall(id="c1", name="effect", arguments={"value": "x"})),
            FinalAction(content="done"),
        ],
    )
    await runtime.run(task_id="task", task="do it", workspace=tmp_path)

    assert seen_states == [InvocationState.STARTED]
    assert tool.executions == 1


async def test_cancellation_stops_the_run_and_leaves_started_call_for_recovery(
    tmp_path: Path,
) -> None:
    journal, checkpoints = _stores(tmp_path)
    entered = asyncio.Event()

    class BlockingTool(CountingTool):
        async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
            del arguments, context
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write("ran\n")
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    tool = BlockingTool(tmp_path / "effects.txt", ToolEffectClass.NON_IDEMPOTENT)
    model = _Scripted(
        [
            ToolAction(call=ToolCall(id="c1", name="effect", arguments={"value": "x"})),
            FinalAction(content="must not run"),
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
        checkpoint_store=checkpoints,
        invocation_journal=journal,
    )
    task = asyncio.create_task(runtime.run(task_id="task", task="do it", workspace=tmp_path))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.STARTED
    assert decide_recovery(record) is RecoveryDecision.REFUSE_REPLAY
    previous = checkpoints.load("task")
    assert previous is not None
    resumed = await runtime.resume_running(previous=previous, workspace=tmp_path)
    assert resumed.status is RunStatus.FAILED
    assert "indeterminate_side_effect" in (resumed.error or "")
    settled = journal.load(run_id="task", logical_call_id="c1")
    assert settled is not None
    assert settled.state is InvocationState.INDETERMINATE
    assert model.calls == 1
    assert tool.executions == 1


@pytest.mark.parametrize(
    ("journalled_state", "effect_class", "expected_reason"),
    [
        (None, ToolEffectClass.NON_IDEMPOTENT, "missing_journal_record"),
        (InvocationState.CLAIMED, ToolEffectClass.NON_IDEMPOTENT, "execute"),
        (InvocationState.STARTED, ToolEffectClass.READ_ONLY, "replay_read_only"),
        (InvocationState.STARTED, ToolEffectClass.IDEMPOTENT, "replay_idempotent"),
    ],
)
async def test_unsupported_recovery_outcomes_fail_without_advancing(
    tmp_path: Path,
    journalled_state: InvocationState | None,
    effect_class: ToolEffectClass,
    expected_reason: str,
) -> None:
    from datetime import UTC, datetime

    from forgeharness.domain.models import Message, MessageRole
    from forgeharness.state.invocation_journal import (
        InvocationRecord,
        args_digest,
        idempotency_key_for,
    )

    journal, checkpoints = _stores(tmp_path)
    call = ToolCall(id="c1", name="effect", arguments={"value": "x"})
    interrupted = checkpoints.save(
        RunResult(
            task_id="task",
            status=RunStatus.RUNNING,
            messages=(
                Message(role=MessageRole.USER, content="do it"),
                Message(role=MessageRole.ASSISTANT, tool_calls=(call,)),
            ),
            usage=Usage(steps=1),
            recovery_semantics_version=CURRENT_RECOVERY_SEMANTICS_VERSION,
        )
    )
    if journalled_state is not None:
        record = InvocationRecord(
            invocation_id="task:c1",
            run_id="task",
            logical_call_id="c1",
            tool_name="effect",
            effect_class=effect_class,
            args_digest=args_digest(call.arguments),
            idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
            state=InvocationState.CLAIMED,
            claimed_at=datetime.now(UTC),
        )
        journal.claim(record)
        if journalled_state is InvocationState.STARTED:
            journal.mark_started(
                record.invocation_id,
                checkpoint_revision=interrupted.checkpoint_revision,
            )

    tool = CountingTool(tmp_path / "effects.txt", effect_class)
    model = _Scripted([FinalAction(content="must not run")])
    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
        checkpoint_store=checkpoints,
        invocation_journal=journal,
    )
    resumed = await runtime.resume_running(previous=interrupted, workspace=tmp_path)

    assert resumed.status is RunStatus.FAILED
    assert "recovery_unsupported" in (resumed.error or "")
    assert expected_reason in (resumed.error or "")
    assert model.calls == 0
    assert tool.executions == 0


async def test_completed_call_is_reused_when_the_checkpoint_was_lost(tmp_path: Path) -> None:
    """Test A: a completed side effect is not repeated after a checkpoint loss.

    The tool runs once; the journal records completion; the process dies before the
    checkpoint records the tool result. On resume the runtime must return the
    journaled observation and must not call the tool again.
    """
    journal, checkpoints = _stores(tmp_path)
    side_effect = tmp_path / "effects.txt"
    tool = CountingTool(side_effect, ToolEffectClass.NON_IDEMPOTENT)

    # First pass: run to completion, then simulate losing the post-dispatch
    # checkpoint by rewinding to the state saved *before* the dispatch.
    runtime = _build(
        tmp_path,
        tool,
        journal=journal,
        checkpoints=checkpoints,
        actions=[
            ToolAction(call=ToolCall(id="c1", name="effect", arguments={"value": "x"})),
            FinalAction(content="done"),
        ],
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    assert result.status is RunStatus.SUCCEEDED
    assert tool.executions == 1

    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.COMPLETED
    assert record.result is not None
    assert record.result.content == "appended one line"

    # Resume from a rewritten checkpoint that still ends on the assistant tool
    # call, i.e. the world in which the tool result was never persisted.
    rewound = result.model_copy(
        update={
            "status": RunStatus.RUNNING,
            "messages": (
                result.messages[0],
                result.messages[1].__class__(
                    role=result.messages[1].role,
                    content="do it",
                ),
                result.messages[2],
            ),
            "final_output": None,
        }
    )
    resumed = await runtime.resume_running(previous=rewound, workspace=tmp_path)

    # The side effect did not run twice, and the run completed from the journal.
    assert tool.executions == 1
    assert resumed.status is RunStatus.SUCCEEDED
    tool_messages = [m for m in resumed.messages if m.role.value == "tool"]
    assert any(m.content == "appended one line" for m in tool_messages)


async def test_started_non_idempotent_refuses_replay_after_side_effect(tmp_path: Path) -> None:
    """Test B2: the side effect happened but `completed` was never written.

    This is the window the pre-journal design cannot handle at all. The transcript
    ends on the tool call (the checkpoint never recorded its result) while the
    journal says `started`, so recovery cannot tell whether the write occurred. It
    must refuse, keeping the effect at exactly one execution and refusing to report
    success.
    """
    from datetime import UTC, datetime

    from forgeharness.domain.models import Message, MessageRole
    from forgeharness.state.invocation_journal import (
        InvocationRecord,
        args_digest,
        idempotency_key_for,
    )

    journal, checkpoints = _stores(tmp_path)
    side_effect = tmp_path / "effects.txt"
    tool = CrashingTool(side_effect, ToolEffectClass.NON_IDEMPOTENT)

    # The side effect occurred, then the process died before `completed`.
    call = ToolCall(id="c1", name="effect", arguments={"value": "x"})
    side_effect.write_text("ran\n")
    record = InvocationRecord(
        invocation_id="task:c1",
        run_id="task",
        logical_call_id="c1",
        tool_name="effect",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest=args_digest(call.arguments),
        idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    journal.mark_started(record.invocation_id, checkpoint_revision=1)
    assert tool.executions == 1

    # The checkpoint is the world before the tool result was written: it ends on
    # the assistant message that requested the call.
    interrupted = RunResult(
        task_id="task",
        status=RunStatus.RUNNING,
        messages=(
            Message(role=MessageRole.USER, content="do it"),
            Message(
                role=MessageRole.ASSISTANT,
                content=None,
                tool_calls=(call,),
            ),
        ),
        usage=Usage(steps=1),
        # Revision 0 makes this the first write for the task, matching a process
        # that died before it could save anything after the dispatch.
        checkpoint_revision=0,
        # The run was journal-backed, so recovery consults the journal rather than
        # the pre-journal fail-closed path.
        recovery_semantics_version=CURRENT_RECOVERY_SEMANTICS_VERSION,
    )
    interrupted = checkpoints.save(interrupted)

    runtime = _build(tmp_path, tool, journal=journal, checkpoints=checkpoints, actions=[])
    resumed = await runtime.resume_running(previous=interrupted, workspace=tmp_path)

    # Not replayed, and not reported as success.
    assert tool.executions == 1
    assert resumed.status is RunStatus.FAILED
    assert "indeterminate_side_effect" in (resumed.error or "")
    assert journal.load(run_id="task", logical_call_id="c1").state is InvocationState.INDETERMINATE


async def test_started_non_idempotent_refuses_replay_even_without_a_side_effect(
    tmp_path: Path,
) -> None:
    """Test B1: the deliberate false positive.

    The crash landed after `started` but before the tool did anything, so a replay
    would in fact have been safe. The journal cannot observe that, so it refuses
    anyway. Availability is traded for safety on purpose.
    """
    journal, checkpoints = _stores(tmp_path)
    side_effect = tmp_path / "effects.txt"
    tool = CountingTool(side_effect, ToolEffectClass.NON_IDEMPOTENT)

    # Hand-build the state the runtime would leave after a crash between the
    # durable `started` commit and the actual tool call.
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import (
        InvocationRecord,
        args_digest,
        idempotency_key_for,
    )

    record = InvocationRecord(
        invocation_id="task:c1",
        run_id="task",
        logical_call_id="c1",
        tool_name="effect",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest=args_digest({"value": "x"}),
        idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    journal.mark_started(record.invocation_id, checkpoint_revision=1)

    assert side_effect.exists() is False  # nothing ran
    runtime = _build(tmp_path, tool, journal=journal, checkpoints=checkpoints, actions=[])
    outcome = runtime.recover_call(task_id="task", logical_call_id="c1")

    assert outcome is not None
    assert outcome.decision is RecoveryDecision.REFUSE_REPLAY
    assert outcome.blocks_resume is True
    assert outcome.record.state is InvocationState.INDETERMINATE
    # Still not executed, and it will not be.
    assert tool.executions == 0


@pytest.mark.parametrize(
    ("effect_class", "journalled_state", "expected"),
    [
        (ToolEffectClass.READ_ONLY, InvocationState.CLAIMED, RecoveryDecision.EXECUTE),
        (ToolEffectClass.NON_IDEMPOTENT, InvocationState.CLAIMED, RecoveryDecision.EXECUTE),
        (ToolEffectClass.READ_ONLY, InvocationState.STARTED, RecoveryDecision.REPLAY_READ_ONLY),
        (ToolEffectClass.IDEMPOTENT, InvocationState.STARTED, RecoveryDecision.REPLAY_IDEMPOTENT),
        (
            ToolEffectClass.NON_IDEMPOTENT,
            InvocationState.STARTED,
            RecoveryDecision.REFUSE_REPLAY,
        ),
        (ToolEffectClass.READ_ONLY, InvocationState.COMPLETED, RecoveryDecision.REUSE_RESULT),
        (ToolEffectClass.IDEMPOTENT, InvocationState.COMPLETED, RecoveryDecision.REUSE_RESULT),
        (ToolEffectClass.NON_IDEMPOTENT, InvocationState.COMPLETED, RecoveryDecision.REUSE_RESULT),
        (ToolEffectClass.NON_IDEMPOTENT, InvocationState.FAILED, RecoveryDecision.REFUSE_REPLAY),
        (
            ToolEffectClass.NON_IDEMPOTENT,
            InvocationState.INDETERMINATE,
            RecoveryDecision.REFUSE_REPLAY,
        ),
    ],
)
def test_recovery_matrix(
    effect_class: ToolEffectClass,
    journalled_state: InvocationState,
    expected: RecoveryDecision,
    tmp_path: Path,
) -> None:
    """The frozen decision matrix from ADR 009, row by row."""
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import InvocationRecord

    record = InvocationRecord(
        invocation_id="i",
        run_id="r",
        logical_call_id="c",
        tool_name="t",
        effect_class=effect_class,
        args_digest="0" * 64,
        idempotency_key="k",
        state=journalled_state,
        claimed_at=datetime.now(UTC),
    )
    assert decide_recovery(record) is expected


@pytest.mark.parametrize(
    ("effect_class", "attempted", "expected"),
    [
        (ToolEffectClass.NON_IDEMPOTENT, False, InvocationState.FAILED),
        (ToolEffectClass.NON_IDEMPOTENT, True, InvocationState.INDETERMINATE),
        (ToolEffectClass.READ_ONLY, True, InvocationState.FAILED),
        (ToolEffectClass.IDEMPOTENT, True, InvocationState.FAILED),
    ],
)
def test_failure_is_classified_by_certainty_not_by_exception(
    effect_class: ToolEffectClass, attempted: bool, expected: InvocationState
) -> None:
    """`failed` requires evidence that no uncertain side effect remains.

    A raised exception is not such evidence, so a non-idempotent tool that began
    executing is `indeterminate`, not `failed`.
    """
    assert (
        terminal_state_for_failure(effect_class=effect_class, execution_attempted=attempted)
        is expected
    )


async def test_journal_records_the_stable_identity_of_a_call(tmp_path: Path) -> None:
    """One logical call carries a stable key and a per-call attempt count."""
    journal, checkpoints = _stores(tmp_path)
    side_effect = tmp_path / "effects.txt"
    tool = CountingTool(side_effect, ToolEffectClass.READ_ONLY)
    runtime = _build(
        tmp_path,
        tool,
        journal=journal,
        checkpoints=checkpoints,
        actions=[
            ToolAction(call=ToolCall(id="c7", name="effect", arguments={"value": "x"})),
            FinalAction(content="done"),
        ],
    )
    await runtime.run(task_id="task", task="do it", workspace=tmp_path)

    record = journal.load(run_id="task", logical_call_id="c7")
    assert record is not None
    assert record.idempotency_key == "task:c7"
    assert record.attempt_count == 1
    assert record.effect_class is ToolEffectClass.READ_ONLY
    assert record.result_digest is not None


async def test_a_run_without_a_journal_still_works(tmp_path: Path) -> None:
    """The journal is opt-in: no journal means no side-effect promise, not a crash."""
    side_effect = tmp_path / "effects.txt"
    tool = CountingTool(side_effect, ToolEffectClass.READ_ONLY)
    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(
        model=_Scripted(
            [
                ToolAction(call=ToolCall(id="c1", name="effect", arguments={})),
                FinalAction(content="done"),
            ]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    assert result.status is RunStatus.SUCCEEDED
    assert tool.executions == 1


async def test_started_read_only_call_is_replayed(tmp_path: Path) -> None:
    """A read-only call found at `started` may safely run again."""
    journal, checkpoints = _stores(tmp_path)
    side_effect = tmp_path / "effects.txt"
    tool = CountingTool(side_effect, ToolEffectClass.READ_ONLY)

    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import (
        InvocationRecord,
        args_digest,
        idempotency_key_for,
    )

    record = InvocationRecord(
        invocation_id="task:c1",
        run_id="task",
        logical_call_id="c1",
        tool_name="effect",
        effect_class=ToolEffectClass.READ_ONLY,
        args_digest=args_digest({}),
        idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    journal.mark_started(record.invocation_id, checkpoint_revision=1)

    runtime = _build(tmp_path, tool, journal=journal, checkpoints=checkpoints, actions=[])
    outcome = runtime.recover_call(task_id="task", logical_call_id="c1")
    assert outcome is not None
    assert outcome.decision is RecoveryDecision.REPLAY_READ_ONLY
    assert outcome.blocks_resume is False


def test_journal_result_is_not_truncated_silently(tmp_path: Path) -> None:
    """Recovery must reproduce the transcript exactly, so a huge result is refused."""
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import (
        MAX_JOURNALED_RESULT_BYTES,
        InvocationRecord,
        JournalError,
        args_digest,
        idempotency_key_for,
        settle_invocation,
    )

    journal, _ = _stores(tmp_path)
    record = InvocationRecord(
        invocation_id="task:c1",
        run_id="task",
        logical_call_id="c1",
        tool_name="effect",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest=args_digest({}),
        idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    journal.mark_started(record.invocation_id, checkpoint_revision=0)
    huge = ToolOutput(ok=True, content="x" * (MAX_JOURNALED_RESULT_BYTES + 10))
    settled = settle_invocation(
        journal.load(run_id="task", logical_call_id="c1"),  # type: ignore[arg-type]
        huge,
        datetime.now(UTC),
    )
    with pytest.raises(JournalError, match="exceeds MAX_JOURNALED_RESULT_BYTES"):
        journal.settle(settled)


def test_settle_refuses_a_non_terminal_state(tmp_path: Path) -> None:
    """A terminal write must not be usable to launder an unfinished invocation."""
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import InvocationRecord, JournalError

    journal, _ = _stores(tmp_path)
    record = InvocationRecord(
        invocation_id="i",
        run_id="r",
        logical_call_id="c",
        tool_name="t",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest="0" * 64,
        idempotency_key="k",
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    with pytest.raises(JournalError, match="non-terminal"):
        journal.settle(record)


def test_claim_twice_for_one_logical_call_is_rejected(tmp_path: Path) -> None:
    """One logical call has exactly one journal row."""
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import InvocationRecord, JournalError

    journal, _ = _stores(tmp_path)
    record = InvocationRecord(
        invocation_id="i1",
        run_id="r",
        logical_call_id="c",
        tool_name="t",
        effect_class=ToolEffectClass.READ_ONLY,
        args_digest="0" * 64,
        idempotency_key="k",
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    duplicate = record.model_copy(update={"invocation_id": "i2"})
    with pytest.raises(JournalError, match="already claimed"):
        journal.claim(duplicate)


def test_mark_started_requires_a_claimed_invocation(tmp_path: Path) -> None:
    from forgeharness.state.invocation_journal import JournalError

    journal, _ = _stores(tmp_path)
    with pytest.raises(JournalError, match="not claimed"):
        journal.mark_started("missing", checkpoint_revision=0)


async def test_non_idempotent_failure_after_execution_is_indeterminate(tmp_path: Path) -> None:
    """A tool that raised mid-write leaves the invocation indeterminate, not failed."""
    journal, checkpoints = _stores(tmp_path)
    side_effect = tmp_path / "effects.txt"
    tool = CrashingTool(side_effect, ToolEffectClass.NON_IDEMPOTENT)
    runtime = _build(
        tmp_path,
        tool,
        journal=journal,
        checkpoints=checkpoints,
        actions=[
            ToolAction(call=ToolCall(id="c1", name="effect", arguments={})),
            FinalAction(content="done"),
        ],
    )
    await runtime.run(task_id="task", task="do it", workspace=tmp_path)

    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.INDETERMINATE
    assert record.error_code == ToolErrorCode.TOOL_EXECUTION_ERROR.value


async def test_read_only_failure_is_failed_not_indeterminate(tmp_path: Path) -> None:
    """A read-only tool has no side effect to be uncertain about."""

    class BrokenRead(CountingTool):
        async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
            del arguments, context
            raise RuntimeError("read failed")

    journal, checkpoints = _stores(tmp_path)
    tool = BrokenRead(tmp_path / "unused.txt", ToolEffectClass.READ_ONLY)
    runtime = _build(
        tmp_path,
        tool,
        journal=journal,
        checkpoints=checkpoints,
        actions=[
            ToolAction(call=ToolCall(id="c1", name="effect", arguments={})),
            FinalAction(content="done"),
        ],
    )
    await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.FAILED


async def test_resume_running_ignores_a_transcript_that_is_not_mid_call(tmp_path: Path) -> None:
    """With no outstanding call there is nothing to recover."""
    journal, checkpoints = _stores(tmp_path)
    tool = CountingTool(tmp_path / "effects.txt", ToolEffectClass.READ_ONLY)
    runtime = _build(tmp_path, tool, journal=journal, checkpoints=checkpoints, actions=[])
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    running = result.model_copy(update={"status": RunStatus.RUNNING, "final_output": None})
    resumed = await runtime.resume_running(previous=running, workspace=tmp_path)
    assert resumed.status is RunStatus.SUCCEEDED
    assert tool.executions == 0


async def test_resume_running_rejects_a_non_running_checkpoint(tmp_path: Path) -> None:
    journal, checkpoints = _stores(tmp_path)
    tool = CountingTool(tmp_path / "effects.txt", ToolEffectClass.READ_ONLY)
    runtime = _build(tmp_path, tool, journal=journal, checkpoints=checkpoints, actions=[])
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    with pytest.raises(ValueError, match="only a running run"):
        await runtime.resume_running(previous=result, workspace=tmp_path)


def test_recovery_is_auditable_in_the_trace(tmp_path: Path) -> None:
    """A recovery decision must be reconstructable, not silent."""
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import (
        InvocationRecord,
        args_digest,
        idempotency_key_for,
    )

    journal, checkpoints = _stores(tmp_path)
    tool = CountingTool(tmp_path / "effects.txt", ToolEffectClass.NON_IDEMPOTENT)
    record = InvocationRecord(
        invocation_id="task:c1",
        run_id="task",
        logical_call_id="c1",
        tool_name="effect",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest=args_digest({}),
        idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    journal.mark_started(record.invocation_id, checkpoint_revision=1)

    trace = InMemoryTrace("task")
    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(
        model=_Scripted([]),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
        checkpoint_store=checkpoints,
        invocation_journal=journal,
    )
    runtime.recover_call(task_id="task", logical_call_id="c1")

    types = [event.type for event in trace.events]
    assert "tool.recovery.decided" in types
    assert "tool.invocation.indeterminate" in types
    decided = next(e for e in trace.events if e.type == "tool.recovery.decided")
    assert decided.payload["decision"] == "refuse_replay"
    assert decided.payload["journal_state"] == "started"


def test_concurrent_sqlite_journals_are_serialised(tmp_path: Path) -> None:
    """Two writers cannot both advance one invocation."""
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import InvocationRecord, JournalError

    db = tmp_path / "runs.sqlite3"
    first = SQLiteInvocationJournal(db)
    second = SQLiteInvocationJournal(db)
    record = InvocationRecord(
        invocation_id="i",
        run_id="r",
        logical_call_id="c",
        tool_name="t",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest="0" * 64,
        idempotency_key="k",
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    first.claim(record)
    first.mark_started("i", checkpoint_revision=0)
    with pytest.raises(JournalError, match="not claimed"):
        second.mark_started("i", checkpoint_revision=0)


def test_asyncio_is_available_for_the_async_surface() -> None:
    """Guard the async test surface this module relies on."""
    assert asyncio.iscoroutinefunction(AgentRuntime.run)


async def test_completed_row_without_a_result_is_refused(tmp_path: Path) -> None:
    """A completed invocation must carry its result; recovery cannot invent one.

    Reaching this state means the journal was corrupted (completed without the
    observation it promises). Recovery must fail loudly rather than call the tool
    again to obtain a result, which is the exact behaviour the journal exists to
    prevent.
    """
    from datetime import UTC, datetime

    from forgeharness.domain.models import Message, MessageRole
    from forgeharness.state.invocation_journal import (
        InvocationRecord,
        args_digest,
        idempotency_key_for,
    )

    journal, checkpoints = _stores(tmp_path)
    tool = CountingTool(tmp_path / "effects.txt", ToolEffectClass.NON_IDEMPOTENT)
    call = ToolCall(id="c1", name="effect", arguments={"value": "x"})
    record = InvocationRecord(
        invocation_id="task:c1",
        run_id="task",
        logical_call_id="c1",
        tool_name="effect",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest=args_digest(call.arguments),
        idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    journal.mark_started(record.invocation_id, checkpoint_revision=1)
    # Force `completed` while leaving `result` empty: the corrupted state.
    with journal._connect() as connection:
        connection.execute(
            "UPDATE tool_invocations SET state = ? WHERE invocation_id = ?",
            (InvocationState.COMPLETED.value, record.invocation_id),
        )
        connection.commit()

    interrupted = RunResult(
        task_id="task",
        status=RunStatus.RUNNING,
        messages=(
            Message(role=MessageRole.USER, content="do it"),
            Message(role=MessageRole.ASSISTANT, content=None, tool_calls=(call,)),
        ),
        usage=Usage(steps=1),
        checkpoint_revision=0,
        recovery_semantics_version=CURRENT_RECOVERY_SEMANTICS_VERSION,
    )
    interrupted = checkpoints.save(interrupted)
    runtime = _build(tmp_path, tool, journal=journal, checkpoints=checkpoints, actions=[])

    with pytest.raises(JournalError, match="completed but has no result"):
        await runtime.resume_running(previous=interrupted, workspace=tmp_path)
    assert tool.executions == 0


async def test_settle_twice_is_refused(tmp_path: Path) -> None:
    """A terminal write cannot be applied twice, so a retry cannot double-settle."""
    from datetime import UTC, datetime

    from forgeharness.state.invocation_journal import (
        InvocationRecord,
        args_digest,
        idempotency_key_for,
        settle_invocation,
    )

    journal, _ = _stores(tmp_path)
    record = InvocationRecord(
        invocation_id="i",
        run_id="r",
        logical_call_id="c",
        tool_name="t",
        effect_class=ToolEffectClass.READ_ONLY,
        args_digest=args_digest({}),
        idempotency_key=idempotency_key_for(run_id="r", logical_call_id="c"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    started = journal.mark_started("i", checkpoint_revision=0)
    settled = settle_invocation(started, ToolOutput(ok=True, content="ok"), datetime.now(UTC))
    journal.settle(settled)
    with pytest.raises(JournalError, match="already terminal"):
        journal.settle(settled)


def test_scalar_coercion_is_strict_about_unexpected_types(tmp_path: Path) -> None:
    """A column of an impossible type must fail loudly, not coerce silently."""
    from forgeharness.state.invocation_journal import JournalError, _as_int

    assert _as_int(3) == 3
    assert _as_int("4") == 4
    assert _as_int(5.0) == 5
    assert _as_int(True) == 0
    assert _as_int(None) == 0
    with pytest.raises(JournalError, match="unexpected journal integer"):
        _as_int([1, 2])


def test_optional_timestamp_parsing() -> None:
    from datetime import datetime

    from forgeharness.state.invocation_journal import _as_datetime

    assert _as_datetime(None) is None
    assert _as_datetime("") is None
    parsed = _as_datetime("2026-09-17T10:00:00+00:00")
    assert isinstance(parsed, datetime)


async def test_pre_journal_run_fails_closed_without_inferring_from_the_trace(
    tmp_path: Path,
) -> None:
    """A run written before the journal existed must not be resumed on a guess.

    ADR 009 forbids reconstructing a journal from trace events: the absence of a
    `tool.completed` event never proves the tool did not run. A missing journal row
    on a pre-journal run therefore means "unknown", and unknown fails closed.
    """
    from forgeharness.domain.models import Message, MessageRole

    _, checkpoints = _stores(tmp_path)
    journal, _ = _stores(tmp_path)
    tool = CountingTool(tmp_path / "effects.txt", ToolEffectClass.NON_IDEMPOTENT)
    call = ToolCall(id="c1", name="effect", arguments={"value": "x"})

    # A pre-journal checkpoint: version 0, no journal rows at all.
    interrupted = RunResult(
        task_id="task",
        status=RunStatus.RUNNING,
        messages=(
            Message(role=MessageRole.USER, content="do it"),
            Message(role=MessageRole.ASSISTANT, content=None, tool_calls=(call,)),
        ),
        usage=Usage(steps=1),
        checkpoint_revision=0,
        recovery_semantics_version=0,
    )
    interrupted = checkpoints.save(interrupted)

    runtime = _build(tmp_path, tool, journal=journal, checkpoints=checkpoints, actions=[])
    resumed = await runtime.resume_running(previous=interrupted, workspace=tmp_path)

    assert resumed.status is RunStatus.FAILED
    assert "indeterminate_side_effect" in (resumed.error or "")
    assert "predates the invocation journal" in (resumed.error or "")
    assert tool.executions == 0


async def test_journal_backed_run_is_stamped_with_the_current_semantics(tmp_path: Path) -> None:
    """The version is stamped by the runtime, so recovery knows what it is reading."""
    journal, checkpoints = _stores(tmp_path)
    tool = CountingTool(tmp_path / "effects.txt", ToolEffectClass.READ_ONLY)
    runtime = _build(
        tmp_path,
        tool,
        journal=journal,
        checkpoints=checkpoints,
        actions=[
            ToolAction(call=ToolCall(id="c1", name="effect", arguments={})),
            FinalAction(content="done"),
        ],
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    assert result.recovery_semantics_version == CURRENT_RECOVERY_SEMANTICS_VERSION
    assert result.status is RunStatus.SUCCEEDED


async def test_run_without_a_journal_is_stamped_version_zero(tmp_path: Path) -> None:
    """A runtime with no journal records that no side-effect record exists."""
    side_effect = tmp_path / "effects.txt"
    tool = CountingTool(side_effect, ToolEffectClass.READ_ONLY)
    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(
        model=_Scripted(
            [
                ToolAction(call=ToolCall(id="c1", name="effect", arguments={})),
                FinalAction(content="done"),
            ]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
        checkpoint_store=SQLiteCheckpointStore(tmp_path / "runs.sqlite3"),
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    assert result.recovery_semantics_version == 0

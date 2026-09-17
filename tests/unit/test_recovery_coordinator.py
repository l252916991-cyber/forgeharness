"""Recovery coordinator: startup orchestration and composition-level crash recovery.

The graduation test here is different in kind from the journal unit tests. It builds
a real `CodingAgent`, lets a real tool produce a real side effect, manufactures a
crash window, rebuilds the entire composition, and recovers through the real entry
point — then asserts the side effect still happened exactly once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from forgeharness.coding.agent import CodingAgent
from forgeharness.domain.models import (
    CURRENT_RECOVERY_SEMANTICS_VERSION,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    RunResult,
    RunStatus,
    ToolCall,
    Usage,
)
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.state.approval import InMemoryApprovalLedger
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.state.invocation_journal import (
    InvocationRecord,
    InvocationState,
    SQLiteInvocationJournal,
    args_digest,
    idempotency_key_for,
)
from forgeharness.state.recovery import RecoveryCoordinator, RecoveryStatus
from forgeharness.tools.base import (
    RiskLevel,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
)


class _ScriptedModel:
    """Return queued actions in order, then finish."""

    def __init__(self, actions: list[object]) -> None:
        self._actions = list(actions)

    async def decide(self, request: object) -> ModelResult:
        del request
        action = self._actions.pop(0) if self._actions else _final()
        return ModelResult(action=action, usage=ModelUsage(input_tokens=5, output_tokens=2))


def _final():
    from forgeharness.domain.models import FinalAction

    return FinalAction(content="done")


def _coordinator(
    *,
    checkpoints: SQLiteCheckpointStore,
    resume,
    workspace: Path,
) -> RecoveryCoordinator:
    return RecoveryCoordinator(
        checkpoints=checkpoints,
        resume=resume,
        resolve_workspace=lambda snapshot: workspace,
    )


async def _noop_resume(snapshot: RunResult, workspace: Path) -> RunResult:
    del workspace
    return snapshot


async def test_scan_returns_terminal_runs_as_recovered_without_acting(tmp_path: Path) -> None:
    """A finished run is not mid-flight, so a repeated startup does no work."""
    checkpoints = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    done = checkpoints.save(
        RunResult(
            task_id="task-done",
            status=RunStatus.SUCCEEDED,
            messages=(Message(role=MessageRole.USER, content="x"),),
            usage=Usage(steps=1),
            final_output="ok",
        )
    )
    coordinator = _coordinator(checkpoints=checkpoints, resume=_noop_resume, workspace=tmp_path)
    # `pending_runs` excludes terminal runs; recovering one explicitly is a no-op.
    result = await coordinator.recover_run("task-done")
    assert result.status is RecoveryStatus.RECOVERED
    assert result.detail == "already terminal: succeeded"
    assert done.status is RunStatus.SUCCEEDED


async def test_awaiting_approval_is_not_treated_as_recoverable(tmp_path: Path) -> None:
    """A run waiting for a human is not a run that needs recovery."""
    checkpoints = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    checkpoints.save(
        RunResult(
            task_id="task-approval",
            status=RunStatus.AWAITING_APPROVAL,
            messages=(Message(role=MessageRole.USER, content="x"),),
            usage=Usage(steps=1),
        )
    )
    assert checkpoints.pending_runs() == ()


async def test_one_bad_run_does_not_block_the_others(tmp_path: Path) -> None:
    """A failing recovery is isolated to its own run; the scan continues."""
    checkpoints = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    for task_id in ("task-a", "task-b", "task-c"):
        checkpoints.save(
            RunResult(
                task_id=task_id,
                status=RunStatus.RUNNING,
                messages=(Message(role=MessageRole.USER, content="x"),),
                usage=Usage(steps=1),
            )
        )
    attempted: list[str] = []

    async def resume(snapshot: RunResult, workspace: Path) -> RunResult:
        del workspace
        attempted.append(snapshot.task_id)
        if snapshot.task_id == "task-b":
            raise RuntimeError("this run is broken")
        return snapshot.model_copy(update={"status": RunStatus.SUCCEEDED, "final_output": "ok"})

    coordinator = _coordinator(checkpoints=checkpoints, resume=resume, workspace=tmp_path)
    results = {item.task_id: item for item in await coordinator.recover_pending()}

    assert attempted == ["task-a", "task-b", "task-c"]
    assert results["task-a"].status is RecoveryStatus.RECOVERED
    assert results["task-b"].status is RecoveryStatus.FAILED
    assert "this run is broken" in results["task-b"].detail
    assert results["task-c"].status is RecoveryStatus.RECOVERED


async def test_a_lost_journal_race_is_reported_as_conflict(tmp_path: Path) -> None:
    """Two processes starting at once must not both recover the same run.

    The journal's uniqueness on (run_id, logical_call_id) is the lock: the loser
    gets a conflict and stops, rather than executing the side effect a second time.
    """
    from forgeharness.state.invocation_journal import JournalError

    checkpoints = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    checkpoints.save(
        RunResult(
            task_id="task-x",
            status=RunStatus.RUNNING,
            messages=(Message(role=MessageRole.USER, content="x"),),
            usage=Usage(steps=1),
        )
    )

    async def resume(snapshot: RunResult, workspace: Path) -> RunResult:
        del snapshot, workspace
        raise JournalError("invocation already claimed: task-x/c1")

    coordinator = _coordinator(checkpoints=checkpoints, resume=resume, workspace=tmp_path)
    result = await coordinator.recover_run("task-x")
    assert result.status is RecoveryStatus.CONFLICT


async def test_a_lost_checkpoint_race_is_reported_as_conflict(tmp_path: Path) -> None:
    from forgeharness.state.checkpoint import CheckpointConflict

    checkpoints = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    checkpoints.save(
        RunResult(
            task_id="task-y",
            status=RunStatus.RUNNING,
            messages=(Message(role=MessageRole.USER, content="x"),),
            usage=Usage(steps=1),
        )
    )

    async def resume(snapshot: RunResult, workspace: Path) -> RunResult:
        del snapshot, workspace
        raise CheckpointConflict("checkpoint revision conflict for task-y")

    coordinator = _coordinator(checkpoints=checkpoints, resume=resume, workspace=tmp_path)
    assert (await coordinator.recover_run("task-y")).status is RecoveryStatus.CONFLICT


async def test_a_refused_recovery_is_reported_as_refused(tmp_path: Path) -> None:
    checkpoints = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    checkpoints.save(
        RunResult(
            task_id="task-z",
            status=RunStatus.RUNNING,
            messages=(Message(role=MessageRole.USER, content="x"),),
            usage=Usage(steps=1),
        )
    )

    async def resume(snapshot: RunResult, workspace: Path) -> RunResult:
        del workspace
        return snapshot.model_copy(
            update={
                "status": RunStatus.FAILED,
                "error": "indeterminate_side_effect: cannot confirm whether write ran",
            }
        )

    coordinator = _coordinator(checkpoints=checkpoints, resume=resume, workspace=tmp_path)
    result = await coordinator.recover_run("task-z")
    assert result.status is RecoveryStatus.REFUSED
    assert result.decision is not None


async def test_recover_unknown_run_reports_failure(tmp_path: Path) -> None:
    checkpoints = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    coordinator = _coordinator(checkpoints=checkpoints, resume=_noop_resume, workspace=tmp_path)
    assert (await coordinator.recover_run("missing")).status is RecoveryStatus.FAILED


# --------------------------------------------------------------------------------------
# Graduation test: real composition, real side effect, real crash window, real recovery.
# --------------------------------------------------------------------------------------


class _WriteInput(BaseModel):
    """Arguments for the counting write tool."""

    model_config = ConfigDict(extra="forbid")
    path: str
    content: str = "x"


class _WritingTool:
    """A non-idempotent tool whose side effect is observable and countable."""

    input_model = _WriteInput

    def __init__(self, counter: Path) -> None:
        self._counter = counter
        self.spec = ToolSpec(
            name="write_counter",
            description="Append one line to the counter file.",
            input_schema=_WriteInput.model_json_schema(),
            risk=RiskLevel.WRITE,
            effect_class=ToolEffectClass.NON_IDEMPOTENT,
        )

    @property
    def executions(self) -> int:
        """How many times the side effect actually ran."""
        return self._counter.read_text().count("\n") if self._counter.exists() else 0

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        with self._counter.open("a", encoding="utf-8") as handle:
            handle.write("ran\n")
        return ToolOutput(ok=True, content="wrote one line")


async def test_full_composition_recovers_without_repeating_the_side_effect(
    tmp_path: Path,
) -> None:
    """The graduation test for M10-A.

    A real tool produces a real side effect; the process then dies between the
    durable `started` commit and `completed`. The whole composition is rebuilt and
    recovery runs through the real entry point. The side effect must not be repeated
    and the run must not be reported as a success.
    """
    counter = tmp_path / "counter.txt"
    runs_db = tmp_path / "runs.sqlite3"
    workspace = tmp_path
    checkpoints = SQLiteCheckpointStore(runs_db)
    journal = SQLiteInvocationJournal(runs_db)

    call = ToolCall(id="c1", name="write_counter", arguments={"path": "a.txt"})

    # --- First "process": the tool's side effect happens, then the crash. ---
    counter.write_text("ran\n")
    record = InvocationRecord(
        invocation_id="task:c1",
        run_id="task",
        logical_call_id="c1",
        tool_name="write_counter",
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
        args_digest=args_digest(call.arguments),
        idempotency_key=idempotency_key_for(run_id="task", logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    journal.mark_started(record.invocation_id, checkpoint_revision=1)

    # The checkpoint is the world before the tool result was persisted.
    checkpoints.save(
        RunResult(
            task_id="task",
            status=RunStatus.RUNNING,
            messages=(
                Message(role=MessageRole.USER, content="write the file"),
                Message(role=MessageRole.ASSISTANT, content=None, tool_calls=(call,)),
            ),
            usage=Usage(steps=1),
            checkpoint_revision=0,
            recovery_semantics_version=CURRENT_RECOVERY_SEMANTICS_VERSION,
        )
    )
    assert counter.read_text().count("\n") == 1

    # --- Second "process": rebuild the composition from durable state only. ---
    tool = _WritingTool(counter)

    def build_agent() -> tuple[CodingAgent, InMemoryTrace]:
        trace = InMemoryTrace("task")
        agent = CodingAgent(
            model=_ScriptedModel([]),
            trace=trace,
            approval_ledger=InMemoryApprovalLedger(),
            checkpoint_store=checkpoints,
            invocation_journal=journal,
            test_command=("true",),
            extra_tools=(tool,),
        )
        return agent, trace

    async def resume(snapshot: RunResult, target: Path) -> RunResult:
        agent, _ = build_agent()
        return await agent.recover(previous=snapshot, workspace=target)

    coordinator = RecoveryCoordinator(
        checkpoints=checkpoints,
        resume=resume,
        resolve_workspace=lambda snapshot: workspace,
    )
    results = await coordinator.recover_pending()

    assert len(results) == 1
    # Refused: the journal cannot confirm whether the write landed.
    assert results[0].status is RecoveryStatus.REFUSED
    # The headline assertion: the side effect did not happen a second time.
    assert tool.executions == 1
    # And the run did not pretend to be complete.
    final = checkpoints.load("task")
    assert final is not None
    assert final.status is RunStatus.FAILED
    assert "indeterminate_side_effect" in (final.error or "")
    # The journal reflects the refusal.
    stored = journal.load(run_id="task", logical_call_id="c1")
    assert stored is not None
    assert stored.state is InvocationState.INDETERMINATE

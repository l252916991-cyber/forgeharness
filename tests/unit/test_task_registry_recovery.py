"""Global task registry and API startup recovery (M10-A.2).

The registry answers one question — given a task id, which workspace holds its
recovery sources — so these tests are about **discovery**, not safety. Safety stays
with the journal: a locatable workspace must not weaken a fail-closed decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from forgeharness.domain.models import (
    CURRENT_RECOVERY_SEMANTICS_VERSION,
    Message,
    MessageRole,
    RunResult,
    RunStatus,
    ToolCall,
    Usage,
)
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.state.invocation_journal import (
    InvocationRecord,
    InvocationState,
    SQLiteInvocationJournal,
    ToolOutput,
    args_digest,
    idempotency_key_for,
    settle_invocation,
)
from forgeharness.state.recovery import RecoveryCoordinator, RecoveryStatus
from forgeharness.state.task_registry import SQLiteTaskRegistry
from forgeharness.tools.base import ToolEffectClass
from forgeharness.tools.paths import WorkspacePathError, resolve_workspace_path


def _crash_state(
    workspace: Path,
    *,
    task_id: str,
    state: str,
    tool: str = "write_counter",
    effect_class: ToolEffectClass = ToolEffectClass.NON_IDEMPOTENT,
    result_content: str = "wrote one line",
    recovery_semantics_version: int = CURRENT_RECOVERY_SEMANTICS_VERSION,
) -> tuple[Path, str]:
    """Leave a workspace mid-flight in the requested journal state.

    Returns the side-effect counter path and the logical call id, so a test can
    assert the effect's execution count after recovery.
    """
    counter = workspace / "counter.txt"
    if state in {"started", "completed"}:
        # The side effect already happened in both cases; `started` means the
        # `completed` record was never written, `completed` means it was.
        counter.write_text("ran\n")
    data = workspace / ".forgeharness"
    (data / "traces").mkdir(parents=True, exist_ok=True)
    runs_db = data / "runs.sqlite3"
    checkpoints = SQLiteCheckpointStore(runs_db)
    journal = SQLiteInvocationJournal(runs_db)

    call = ToolCall(id="c1", name=tool, arguments={"path": "a.txt"})
    record = InvocationRecord(
        invocation_id=f"{task_id}:c1",
        run_id=task_id,
        logical_call_id="c1",
        tool_name=tool,
        effect_class=effect_class,
        args_digest=args_digest(call.arguments),
        idempotency_key=idempotency_key_for(run_id=task_id, logical_call_id="c1"),
        state=InvocationState.CLAIMED,
        claimed_at=datetime.now(UTC),
    )
    journal.claim(record)
    started = journal.mark_started(record.invocation_id, checkpoint_revision=1)
    if state == "completed":
        journal.settle(
            settle_invocation(
                started, ToolOutput(ok=True, content=result_content), datetime.now(UTC)
            )
        )
    checkpoints.save(
        RunResult(
            task_id=task_id,
            status=RunStatus.RUNNING,
            messages=(
                Message(role=MessageRole.USER, content="write it"),
                Message(role=MessageRole.ASSISTANT, content=None, tool_calls=(call,)),
            ),
            usage=Usage(steps=1),
            checkpoint_revision=0,
            recovery_semantics_version=recovery_semantics_version,
        )
    )
    return counter, "c1"


def _resume_factory(workspace_root: Path):
    """Build a resume callable bound to the right workspace per task id."""

    async def resume(snapshot: RunResult, workspace: Path) -> RunResult:
        del snapshot, workspace
        raise AssertionError("the API recovery test supplies its own resumer")

    return resume


# --------------------------------------------------------------------------------------
# Registry as a locator
# --------------------------------------------------------------------------------------


def test_registry_binds_and_reads_back(tmp_path: Path) -> None:
    registry = SQLiteTaskRegistry(tmp_path / "registry.sqlite")
    first = registry.bind(task_id="t1", workspace="repo-a")
    assert registry.get("t1") is not None
    assert registry.get("t1").workspace == "repo-a"  # type: ignore[union-attr]

    # Re-binding updates the locator but preserves the original creation time.
    registry.bind(task_id="t1", workspace="repo-b")
    updated = registry.get("t1")
    assert updated is not None
    assert updated.workspace == "repo-b"
    assert updated.created_at == first.created_at


def test_registry_returns_none_for_an_unknown_task(tmp_path: Path) -> None:
    registry = SQLiteTaskRegistry(tmp_path / "registry.sqlite")
    assert registry.get("missing") is None
    assert registry.all_bindings() == ()


def test_registry_holds_no_authoritative_status(tmp_path: Path) -> None:
    """The registry is a locator; status has exactly one authority: the checkpoint."""
    from forgeharness.state.task_registry import TaskBinding

    assert set(TaskBinding.model_fields) == {"task_id", "workspace", "created_at", "updated_at"}


# --------------------------------------------------------------------------------------
# Startup discovery
# --------------------------------------------------------------------------------------


async def test_startup_recovery_finds_workspace_and_reuses_completed_result(
    tmp_path: Path,
) -> None:
    """Discovery + reuse: a completed call whose checkpoint was lost is not rerun.

    This is the structural closure M10-A was missing: the service can find a
    leftover task's workspace without being told, and reuses the journaled result.
    """
    workspace_root = tmp_path / "root"
    workspace = workspace_root / "repo-a"
    workspace.mkdir(parents=True)
    counter, call_id = _crash_state(workspace, task_id="task-a", state="completed")

    registry = SQLiteTaskRegistry(tmp_path / "registry.sqlite")
    registry.bind(task_id="task-a", workspace="repo-a")
    checkpoints = SQLiteCheckpointStore(workspace / ".forgeharness" / "runs.sqlite3")
    journal = SQLiteInvocationJournal(workspace / ".forgeharness" / "runs.sqlite3")

    executed: list[str] = []

    async def resume(snapshot: RunResult, target: Path) -> RunResult:
        del target
        # Reuse path: the journal holds the result, so nothing is executed.
        record = journal.load(run_id=snapshot.task_id, logical_call_id=call_id)
        assert record is not None and record.result is not None
        executed.append("reused")
        return snapshot.model_copy(
            update={
                "status": RunStatus.SUCCEEDED,
                "final_output": record.result.content,
                # The reused observation is appended as the tool message the
                # checkpoint was missing.
                "messages": (
                    *snapshot.messages,
                    Message(
                        role=MessageRole.TOOL,
                        content=record.result.content,
                        tool_call_id=call_id,
                        tool_name="write_counter",
                    ),
                ),
            }
        )

    coordinator = RecoveryCoordinator(
        checkpoints=checkpoints,
        resume=resume,
        resolve_workspace=lambda snapshot: workspace,
    )
    results = await coordinator.recover_run("task-a")

    assert results.status is RecoveryStatus.RECOVERED
    assert executed == ["reused"]
    # The side effect did not run twice.
    assert counter.read_text().count("\n") == 1


async def test_startup_recovery_refuses_indeterminate_without_failing_the_service(
    tmp_path: Path,
) -> None:
    """Discovery must not weaken safety: a locatable task can still be refused."""
    workspace_root = tmp_path / "root"
    workspace = workspace_root / "repo-b"
    workspace.mkdir(parents=True)
    counter, _ = _crash_state(workspace, task_id="task-b", state="started")

    checkpoints = SQLiteCheckpointStore(workspace / ".forgeharness" / "runs.sqlite3")
    journal = SQLiteInvocationJournal(workspace / ".forgeharness" / "runs.sqlite3")

    async def resume(snapshot: RunResult, target: Path) -> RunResult:
        del target
        record = journal.load(run_id=snapshot.task_id, logical_call_id="c1")
        assert record is not None
        # A started non-idempotent call is refused, exactly as the journal matrix says.
        return snapshot.model_copy(
            update={
                "status": RunStatus.FAILED,
                "error": "indeterminate_side_effect: cannot confirm whether write ran",
            }
        )

    coordinator = RecoveryCoordinator(
        checkpoints=checkpoints, resume=resume, resolve_workspace=lambda snapshot: workspace
    )
    result = await coordinator.recover_run("task-b")

    assert result.status is RecoveryStatus.REFUSED
    assert counter.read_text().count("\n") == 1


def test_a_binding_outside_the_workspace_root_is_rejected(tmp_path: Path) -> None:
    """The stored path is a locator, never an authorization.

    A corrupted or tampered registry must not be able to point recovery at an
    arbitrary directory, so the value is re-resolved against the configured root.
    """
    workspace_root = tmp_path / "root"
    (workspace_root / "repo-a").mkdir(parents=True)
    registry = SQLiteTaskRegistry(tmp_path / "registry.sqlite")
    registry.bind(task_id="good", workspace="repo-a")
    registry.bind(task_id="bad", workspace="../../etc")

    recovered: list[str] = []
    for binding in registry.all_bindings():
        try:
            resolve_workspace_path(workspace_root, binding.workspace, must_exist=True)
        except (WorkspacePathError, FileNotFoundError):
            continue
        recovered.append(binding.task_id)

    # The escaping binding is dropped; the valid one survives. One bad entry must
    # not stop the others.
    assert recovered == ["good"]


async def test_recovery_is_idempotent_across_repeated_startups(tmp_path: Path) -> None:
    """A second startup must not produce a second side effect."""
    workspace_root = tmp_path / "root"
    workspace = workspace_root / "repo-c"
    workspace.mkdir(parents=True)
    counter, _ = _crash_state(workspace, task_id="task-c", state="completed")
    checkpoints = SQLiteCheckpointStore(workspace / ".forgeharness" / "runs.sqlite3")
    journal = SQLiteInvocationJournal(workspace / ".forgeharness" / "runs.sqlite3")

    async def resume(snapshot: RunResult, target: Path) -> RunResult:
        del target
        record = journal.load(run_id=snapshot.task_id, logical_call_id="c1")
        assert record is not None
        # The real runtime persists the terminal state through the checkpoint store,
        # using the revision it loaded so the CAS accepts it. The double must do the
        # same, or a second startup would still see this run as pending.
        current = checkpoints.load(snapshot.task_id)
        assert current is not None
        return checkpoints.save(
            current.model_copy(
                update={
                    "status": RunStatus.SUCCEEDED,
                    "final_output": record.result.content if record.result else "",
                }
            )
        )

    coordinator = RecoveryCoordinator(
        checkpoints=checkpoints, resume=resume, resolve_workspace=lambda snapshot: workspace
    )
    first = await coordinator.recover_run("task-c")
    assert first.status is RecoveryStatus.RECOVERED
    assert counter.read_text().count("\n") == 1

    # The checkpoint is terminal now, so a second startup does no work at all.
    assert checkpoints.pending_runs() == ()
    second = await coordinator.recover_run("task-c")
    assert second.status is RecoveryStatus.RECOVERED
    assert second.detail.startswith("already terminal")
    assert counter.read_text().count("\n") == 1


async def test_pre_journal_task_stays_fail_closed_even_when_locatable(tmp_path: Path) -> None:
    """Knowing the workspace must not lower the safety bar for an old run.

    A version-0 run has no durable side-effect record, so recovery still refuses
    rather than inferring from the trace.
    """
    from forgeharness.domain.models import ModelResult
    from forgeharness.observability.trace import InMemoryTrace
    from forgeharness.runtime.loop import AgentRuntime
    from forgeharness.state.approval import InMemoryApprovalLedger
    from forgeharness.tools.builtin import EchoTool
    from forgeharness.tools.dispatcher import ToolDispatcher
    from forgeharness.tools.policy import RiskBasedPolicy
    from forgeharness.tools.registry import ToolRegistry

    workspace = tmp_path / "repo-d"
    workspace.mkdir(parents=True)
    runs_db = workspace / ".forgeharness" / "runs.sqlite3"
    checkpoints = SQLiteCheckpointStore(runs_db)
    journal = SQLiteInvocationJournal(runs_db)

    call = ToolCall(id="c1", name="echo", arguments={"text": "x"})
    checkpoints.save(
        RunResult(
            task_id="task-d",
            status=RunStatus.RUNNING,
            messages=(
                Message(role=MessageRole.USER, content="do it"),
                Message(role=MessageRole.ASSISTANT, content=None, tool_calls=(call,)),
            ),
            usage=Usage(steps=1),
            checkpoint_revision=0,
            # Written before the journal existed.
            recovery_semantics_version=0,
        )
    )

    class _Unused:
        async def decide(self, request: object) -> ModelResult:
            del request
            raise AssertionError("a pre-journal run must not call the model")

    registry = ToolRegistry()
    registry.register(EchoTool())
    runtime = AgentRuntime(
        model=_Unused(),  # type: ignore[arg-type]
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=InMemoryTrace("task-d"),
        approval_ledger=InMemoryApprovalLedger(),
        checkpoint_store=checkpoints,
        invocation_journal=journal,
    )
    resumed = await runtime.resume_running(previous=checkpoints.load("task-d"), workspace=workspace)  # type: ignore[arg-type]

    assert resumed.status is RunStatus.FAILED
    assert "predates the invocation journal" in (resumed.error or "")


# --------------------------------------------------------------------------------------
# API startup recovery through the real app composition
# --------------------------------------------------------------------------------------


async def test_api_startup_recovery_refuses_and_does_not_fail_startup(tmp_path: Path) -> None:
    """The real app recovers a leftover task at startup and still serves.

    Recovery is not allowed to break boot: a refused run is a per-run outcome, not a
    startup failure.
    """
    import httpx

    from forgeharness.api import create_app
    from forgeharness.knowledge.application import KnowledgeSettings

    root = tmp_path / "root"
    workspace = root / "repo-x"
    workspace.mkdir(parents=True)
    counter, _ = _crash_state(workspace, task_id="task-x", state="started")

    registry_path = tmp_path / "registry.sqlite"
    SQLiteTaskRegistry(registry_path).bind(task_id="task-x", workspace="repo-x")

    app = create_app(
        tmp_path / "appdata",
        settings=KnowledgeSettings(coding_workspace_root=root, enable_omlx=False),
        task_registry=SQLiteTaskRegistry(registry_path),
        recover_on_startup=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        # Entering the app runs the startup scan, then the app must answer.
        async with app.router.lifespan_context(app):
            health = await client.get("/health")
            assert health.status_code == 200

    checkpoints = SQLiteCheckpointStore(workspace / ".forgeharness" / "runs.sqlite3")
    final = checkpoints.load("task-x")
    assert final is not None
    assert final.status is RunStatus.FAILED
    assert "indeterminate_side_effect" in (final.error or "")
    # The side effect is untouched.
    assert counter.read_text().count("\n") == 1


async def test_api_startup_recovery_is_off_by_default(tmp_path: Path) -> None:
    """A read-only or test instance must not resume runs unless asked."""
    import httpx

    from forgeharness.api import create_app
    from forgeharness.knowledge.application import KnowledgeSettings

    root = tmp_path / "root"
    workspace = root / "repo-y"
    workspace.mkdir(parents=True)
    _crash_state(workspace, task_id="task-y", state="started")
    registry_path = tmp_path / "registry.sqlite"
    SQLiteTaskRegistry(registry_path).bind(task_id="task-y", workspace="repo-y")

    app = create_app(
        tmp_path / "appdata",
        settings=KnowledgeSettings(coding_workspace_root=root, enable_omlx=False),
        task_registry=SQLiteTaskRegistry(registry_path),
        # recover_on_startup defaults to False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        async with app.router.lifespan_context(app):
            assert (await client.get("/health")).status_code == 200

    checkpoints = SQLiteCheckpointStore(workspace / ".forgeharness" / "runs.sqlite3")
    untouched = checkpoints.load("task-y")
    assert untouched is not None
    # Still mid-flight: nothing resumed it.
    assert untouched.status is RunStatus.RUNNING


async def test_startup_recovery_without_a_workspace_root_does_nothing(tmp_path: Path) -> None:
    """With coding disabled there is no root to validate bindings against."""
    from forgeharness.api import _recover_leftover_tasks

    registry = SQLiteTaskRegistry(tmp_path / "registry.sqlite")
    registry.bind(task_id="task-z", workspace="repo-z")
    results = await _recover_leftover_tasks(
        registry=registry,
        workspace_root=None,
        base_url="http://127.0.0.1:1/v1",
        model_name="none",
        api_key="none",
        timeout_seconds=1.0,
    )
    assert results == ()


async def test_startup_recovery_skips_a_binding_that_escapes_the_root(tmp_path: Path) -> None:
    """A tampered binding is reported and skipped; valid tasks still recover."""
    from forgeharness.api import _recover_leftover_tasks

    root = tmp_path / "root"
    (root / "repo-ok").mkdir(parents=True)
    _crash_state(root / "repo-ok", task_id="task-ok", state="started")

    registry = SQLiteTaskRegistry(tmp_path / "registry.sqlite")
    registry.bind(task_id="task-bad", workspace="../../etc")
    registry.bind(task_id="task-ok", workspace="repo-ok")

    results = await _recover_leftover_tasks(
        registry=registry,
        workspace_root=root,
        base_url="http://127.0.0.1:1/v1",
        model_name="none",
        api_key="none",
        timeout_seconds=1.0,
    )
    by_id = {item.task_id: item for item in results}
    # The escaping binding is rejected as a locator, not trusted.
    assert by_id["task-bad"].status is RecoveryStatus.FAILED
    assert "binding rejected" in by_id["task-bad"].detail
    # The valid task was attempted and refused on its own merits.
    assert by_id["task-ok"].status is RecoveryStatus.REFUSED

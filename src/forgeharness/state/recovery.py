"""Recovery orchestration for runs left mid-flight.

This layer is deliberately thin: it selects candidate runs, checks the semantics
generation, delegates to the runtime's ``resume_running``, and records the outcome.
It contains **no** side-effect safety logic. Duplicating the recovery decision
matrix here would create a second source of truth that could disagree with the
journal, which is exactly the failure mode ADR 009 exists to prevent.

Concurrency is handled by the stores that already own it rather than by a lease:
the journal's ``UNIQUE (run_id, logical_call_id)`` refuses a second claim of the
same logical call, and the checkpoint's optimistic revision refuses a second write.
A recovery that loses either race is reported as `conflict` and stops, so two
processes starting at once cannot both execute the same side effect.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path

from forgeharness.domain.models import (
    FrozenModel,
    RunResult,
    RunStatus,
)
from forgeharness.state.checkpoint import CheckpointConflict, CheckpointStore
from forgeharness.state.invocation_journal import JournalError, RecoveryDecision


class RecoveryStatus(StrEnum):
    """Outcome of one run's recovery attempt."""

    # The run was resumed and reached a terminal state.
    RECOVERED = "recovered"
    # Refused on purpose because the side effect's occurrence is unknown.
    REFUSED = "refused"
    # Another process claimed the run (journal or checkpoint CAS refused this one).
    CONFLICT = "conflict"
    # Recovery raised; recorded so one bad run cannot stop the scan.
    FAILED = "failed"


class RunRecoveryResult(FrozenModel):
    """What recovery did with one run, and why."""

    task_id: str
    status: RecoveryStatus
    detail: str = ""
    run_status: RunStatus | None = None
    decision: RecoveryDecision | None = None


# Resumes one run. Composition supplies this: the coordinator must not know how to
# build a model client, only that a run can be resumed.
RunResumer = Callable[[RunResult, Path], Awaitable[RunResult]]
# Resolves a run's workspace, so recovery can bind a runtime to the right directory.
WorkspaceResolver = Callable[[RunResult], Path]


class RecoveryCoordinator:
    """Scan for mid-flight runs and hand each to the runtime's recovery path."""

    def __init__(
        self,
        *,
        checkpoints: CheckpointStore,
        resume: RunResumer,
        resolve_workspace: WorkspaceResolver,
    ) -> None:
        self._checkpoints = checkpoints
        self._resume = resume
        self._resolve_workspace = resolve_workspace

    async def recover_pending(self) -> tuple[RunRecoveryResult, ...]:
        """Attempt every mid-flight run, isolating failures to their own run.

        A run that raises is recorded as `failed` and the scan continues: one
        unrecoverable task must not prevent the others from being recovered.
        """
        return tuple([await self.recover_run(snapshot.task_id) for snapshot in self._pending()])

    async def recover_run(self, task_id: str) -> RunRecoveryResult:
        """Attempt recovery for one run and report what happened."""
        snapshot = self._checkpoints.load(task_id)
        if snapshot is None:
            return RunRecoveryResult(
                task_id=task_id, status=RecoveryStatus.FAILED, detail="run not found"
            )
        if snapshot.status is not RunStatus.RUNNING:
            # Not mid-flight: nothing to recover. Also covers a run another process
            # already finished, so a repeated startup does no work.
            return RunRecoveryResult(
                task_id=task_id,
                status=RecoveryStatus.RECOVERED,
                detail=f"already terminal: {snapshot.status.value}",
                run_status=snapshot.status,
            )
        try:
            resumed = await self._resume(snapshot, self._resolve_workspace(snapshot))
        except CheckpointConflict as exc:
            # Lost the checkpoint race: another process is recovering this run.
            return RunRecoveryResult(
                task_id=task_id, status=RecoveryStatus.CONFLICT, detail=str(exc)
            )
        except JournalError as exc:
            # Lost the invocation race: the journal already holds this logical call,
            # so this process must not touch it.
            return RunRecoveryResult(
                task_id=task_id, status=RecoveryStatus.CONFLICT, detail=str(exc)
            )
        except Exception as exc:
            return RunRecoveryResult(
                task_id=task_id,
                status=RecoveryStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if resumed.status is RunStatus.FAILED and "indeterminate_side_effect" in (
            resumed.error or ""
        ):
            return RunRecoveryResult(
                task_id=task_id,
                status=RecoveryStatus.REFUSED,
                detail=resumed.error or "",
                run_status=resumed.status,
                decision=RecoveryDecision.REFUSE_REPLAY,
            )
        return RunRecoveryResult(
            task_id=task_id,
            status=RecoveryStatus.RECOVERED,
            detail="resumed",
            run_status=resumed.status,
        )

    def _pending(self) -> tuple[RunResult, ...]:
        """Return candidates for recovery.

        ``pending_runs`` filters on the checkpointed status, and only the RUNNING
        status is treated as mid-flight. A run waiting for approval is waiting for a
        human, not for recovery.
        """
        return self._checkpoints.pending_runs()

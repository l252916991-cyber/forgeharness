"""Project an execution trace into ordered, step-level diagnostic records.

This is the **single** trace→step projection for the project. The agent
evaluator, `forge trace-steps`, and the planned bad-case/replay workflow all
consume it, so the step semantics are defined once instead of being re-parsed
from JSONL in several places.

Three identities are deliberately distinct, because M10 introduces retries:

* ``logical_call_id`` — one tool-call *decision* the model emitted. Stable
  across retries and read from the trace's ``call_id``.
* ``attempt_id`` — one actual execution attempt. Today a logical call maps to
  exactly one attempt; under retry it will map to several.
* ``step_id`` — display order only. Never use it to join records.

Two latency figures are reported separately and are never conflated:

* ``model_latency_ms_derived`` — computed from the gap between a ``model.request``
  event and its action. Named "derived" because no model SDK reported it.
* ``tool_latency_ms`` — measured by the dispatcher around the tool call.

An unresolved call is reported as ``incomplete`` rather than omitted or assumed
successful: a model decision that a budget gate refused appears as a model step
plus a tool step that never executed.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from forgeharness.domain.models import FrozenModel, TraceEvent
from forgeharness.observability.hash_chain import verify_trace


class StepPhase(StrEnum):
    """Which stage of the run produced a step."""

    PLANNER = "planner"
    EXECUTOR = "executor"


class StepActionKind(StrEnum):
    """What the harness did at this step."""

    MODEL = "model"
    TOOL = "tool"
    FINISH = "finish"
    ERROR = "error"


class StepStatus(StrEnum):
    """Outcome of one step."""

    COMPLETED = "completed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    AWAITING_APPROVAL = "awaiting_approval"
    INCOMPLETE = "incomplete"


# Terminal run statuses map onto step statuses so a reader sees the same reason
# a run stopped whether they look at the run or at its final step.
_TERMINAL_STEP_STATUS = {
    "succeeded": StepStatus.COMPLETED,
    "failed": StepStatus.FAILED,
    "exhausted": StepStatus.EXHAUSTED,
    "awaiting_approval": StepStatus.AWAITING_APPROVAL,
    "cancelled": StepStatus.FAILED,
    "running": StepStatus.INCOMPLETE,
}


class StepRecord(FrozenModel):
    """One projected execution step, safe to render or assert against."""

    step_id: int = Field(ge=1)
    phase: StepPhase
    action_kind: StepActionKind
    logical_call_id: str | None = None
    attempt_id: str | None = None

    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_ok: bool | None = None
    observation: str | None = None

    model_input_tokens: int = Field(default=0, ge=0)
    model_output_tokens: int = Field(default=0, ge=0)
    # Derived from adjacent event timestamps, not reported by a model SDK.
    model_latency_ms_derived: float | None = Field(default=None, ge=0)
    # Measured by the dispatcher around the tool call.
    tool_latency_ms: int | None = Field(default=None, ge=0)

    error_type: str | None = None
    checkpoint_revision: int | None = Field(default=None, ge=0)
    budget_snapshot: dict[str, int] | None = None

    status: StepStatus


class RunProjection(FrozenModel):
    """All steps for one run plus the aggregates derived from the same pass.

    Two call sequences are exposed because they genuinely differ: a budget gate
    can refuse a decision the model already made. Ordering assertions want the
    decision sequence; execution accounting wants the executed one.
    """

    steps: tuple[StepRecord, ...]
    # Tools the model asked for, in decision order, including refused decisions.
    tool_sequence: tuple[str, ...]
    tool_arguments: tuple[dict[str, Any], ...]
    # Tools that actually ran, in execution order. Shorter than tool_sequence
    # when a budget gate or policy refused a decision.
    executed_sequence: tuple[str, ...]
    # Model decisions that requested a tool, including any a budget gate refused.
    model_decisions: int = Field(ge=0)
    # Execution attempts actually started. Equal to model_decisions minus refused
    # or unresolved decisions, plus retries once M10 lands.
    tool_attempts: int = Field(ge=0)
    plan_events: int = Field(ge=0)
    # Planner-phase model requests, counted whether or not a valid plan resulted.
    # This is a wiring fact; `plan_events` is a quality-dependent one.
    planner_requests: int = Field(ge=0)
    # Every model request the harness issued, including one whose response was
    # rejected before an action existed. `usage.steps` counts completed decisions,
    # so a rejected request makes the two disagree; the gap is reported rather
    # than smoothed over.
    model_requests: int = Field(ge=0)
    incomplete_calls: int = Field(ge=0)
    final_status: StepStatus

    def steps_for(self, kind: StepActionKind) -> tuple[StepRecord, ...]:
        """Return the steps of one action kind, in execution order."""
        return tuple(step for step in self.steps if step.action_kind is kind)


class TraceProjectionError(ValueError):
    """Raised when a trace cannot be projected."""


def project_events(events: tuple[TraceEvent, ...] | list[TraceEvent]) -> RunProjection:
    """Project a sequence of trace events into ordered step records.

    Deterministic: the same events always yield the same records, because every
    derived value comes from the events themselves and never from wall-clock
    time at projection time.
    """
    builder = _Builder()
    for event in events:
        builder.consume(event)
    return builder.finish()


def project_trace_path(path: Path) -> RunProjection:
    """Verify a hash-chained JSONL trace and project it.

    Refuses to project a trace whose chain does not verify: a tampered or
    truncated file must not produce authoritative-looking diagnostics.
    """
    verification = verify_trace(path)
    if not verification.valid:
        raise TraceProjectionError(f"cannot project invalid trace: {verification.error}")
    events: list[TraceEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = json.loads(line)
        events.append(TraceEvent.model_validate(envelope["event"]))
    return project_events(events)


class _Builder:
    """Mutable accumulator; frozen records are produced only at `finish`."""

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []
        # Index into `_steps` of the tool step owed to each logical call, so a
        # completion can update it in place and ordering stays truthful.
        self._tool_index_by_call: dict[str, int] = {}
        self._decision_tools: list[str] = []
        self._decision_args: list[dict[str, Any]] = []
        self._executed_tools: list[str] = []
        self._model_decisions = 0
        self._attempts = 0
        self._plan_events = 0
        self._planner_requests = 0
        self._model_requests = 0
        self._revision: int | None = None
        self._pending_request_at: dict[str, datetime] = {}
        self._final_status = StepStatus.INCOMPLETE

    def consume(self, event: TraceEvent) -> None:
        """Fold one trace event into the projection."""
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            handler(self, event)

    def _append(self, **fields: Any) -> int:
        self._steps.append(fields)
        return len(self._steps) - 1

    def _on_checkpoint(self, event: TraceEvent) -> None:
        revision = event.payload.get("revision")
        if isinstance(revision, int):
            self._revision = revision

    def _on_model_request(self, event: TraceEvent) -> None:
        phase = _phase_of(event)
        self._pending_request_at[phase.value] = event.timestamp
        self._model_requests += 1
        if phase is StepPhase.PLANNER:
            self._planner_requests += 1

    def _on_model_action(self, event: TraceEvent) -> None:
        payload = event.payload
        action = payload.get("action")
        if not isinstance(action, dict):
            raise TraceProjectionError("model.action event has no action payload")
        phase = StepPhase.EXECUTOR
        latency = self._take_latency(phase, event)
        usage_in = _as_int(payload.get("input_tokens"))
        usage_out = _as_int(payload.get("output_tokens"))
        if action.get("kind") == "tool":
            call = action.get("call")
            if not isinstance(call, dict):
                raise TraceProjectionError("tool action has no call payload")
            call_id = str(call.get("id", ""))
            tool_name = str(call.get("name", ""))
            arguments = dict(call.get("arguments", {}))
            self._model_decisions += 1
            self._decision_tools.append(tool_name)
            self._decision_args.append(arguments)
            self._append(
                phase=phase,
                action_kind=StepActionKind.MODEL,
                logical_call_id=call_id,
                tool_name=tool_name,
                tool_args=arguments,
                model_input_tokens=usage_in,
                model_output_tokens=usage_out,
                model_latency_ms_derived=latency,
                checkpoint_revision=self._revision,
                budget_snapshot=_budget_of(payload),
                status=StepStatus.COMPLETED,
            )
            # The execution this decision owes is recorded now as incomplete and
            # upgraded only when real evidence arrives.
            index = self._append(
                phase=phase,
                action_kind=StepActionKind.TOOL,
                logical_call_id=call_id,
                attempt_id=_attempt_id(call_id, 1),
                tool_name=tool_name,
                tool_args=arguments,
                checkpoint_revision=self._revision,
                status=StepStatus.INCOMPLETE,
            )
            self._tool_index_by_call[call_id] = index
            return
        self._append(
            phase=phase,
            action_kind=StepActionKind.MODEL,
            model_input_tokens=usage_in,
            model_output_tokens=usage_out,
            model_latency_ms_derived=latency,
            checkpoint_revision=self._revision,
            budget_snapshot=_budget_of(payload),
            status=StepStatus.COMPLETED,
        )

    def _on_plan_created(self, event: TraceEvent) -> None:
        payload = event.payload
        self._plan_events += 1
        self._append(
            phase=StepPhase.PLANNER,
            action_kind=StepActionKind.MODEL,
            model_input_tokens=_as_int(payload.get("input_tokens")),
            model_output_tokens=_as_int(payload.get("output_tokens")),
            model_latency_ms_derived=self._take_latency(StepPhase.PLANNER, event),
            checkpoint_revision=self._revision,
            status=StepStatus.COMPLETED,
        )

    def _on_plan_failed(self, event: TraceEvent) -> None:
        self._append(
            phase=StepPhase.PLANNER,
            action_kind=StepActionKind.ERROR,
            error_type=str(event.payload.get("error", "plan.failed")),
            model_latency_ms_derived=self._take_latency(StepPhase.PLANNER, event),
            checkpoint_revision=self._revision,
            status=StepStatus.FAILED,
        )

    def _on_tool_completed(self, event: TraceEvent) -> None:
        payload = event.payload
        call_id = str(payload.get("call_id", ""))
        tool_name = str(payload.get("tool", ""))
        ok = bool(payload.get("ok"))
        elapsed = payload.get("elapsed_ms")
        existing = self._tool_index_by_call.pop(call_id, None) if call_id else None
        arguments = (
            dict(self._steps[existing].get("tool_args") or {}) if existing is not None else {}
        )
        self._attempts += 1
        self._executed_tools.append(tool_name)
        fields: dict[str, Any] = {
            "phase": StepPhase.EXECUTOR,
            "action_kind": StepActionKind.TOOL,
            "logical_call_id": call_id or None,
            "attempt_id": _attempt_id(call_id, 1) if call_id else None,
            "tool_name": tool_name,
            "tool_args": arguments,
            "tool_ok": ok,
            "observation": payload.get("observation"),
            "tool_latency_ms": _as_int(elapsed) if elapsed is not None else None,
            "error_type": None if ok else _error_type_of(payload),
            "checkpoint_revision": self._revision,
            "budget_snapshot": _budget_of(payload),
            "status": StepStatus.COMPLETED if ok else StepStatus.FAILED,
        }
        if existing is None:
            # No matching decision in this trace: the call was approved and
            # resumed, so its decision lives in an earlier run's trace.
            self._append(**fields)
            return
        self._steps[existing] = fields

    def _on_policy_decided(self, event: TraceEvent) -> None:
        """Close an owed tool step when policy denies it.

        ``policy.decided`` fires for every decision, so only a denial resolves the
        pending step; allow and require-approval outcomes leave it to the
        execution or suspension paths.
        """
        if event.payload.get("decision") != "deny":
            return
        self._reject_owed_step(str(event.payload.get("call_id", "")), "policy_denied")

    def _on_tool_rejected(self, event: TraceEvent) -> None:
        """A decision that never reached execution because the tool is unknown."""
        self._reject_owed_step(str(event.payload.get("call_id", "")), "unknown_tool")

    def _reject_owed_step(self, call_id: str, reason: str) -> None:
        if not call_id:
            return
        index = self._tool_index_by_call.pop(call_id, None)
        if index is None:
            return
        self._steps[index].update(
            {
                "tool_ok": False,
                "error_type": reason,
                "status": StepStatus.FAILED,
            }
        )

    def _on_awaiting_approval(self, event: TraceEvent) -> None:
        """Mark the owed call as suspended: it is pending, not unaccounted for."""
        call_id = str(event.payload.get("call_id", ""))
        if not call_id:
            return
        index = self._tool_index_by_call.pop(call_id, None)
        if index is None:
            return
        self._steps[index]["status"] = StepStatus.AWAITING_APPROVAL

    def _on_model_failed(self, event: TraceEvent) -> None:
        self._append(
            phase=StepPhase.EXECUTOR,
            action_kind=StepActionKind.ERROR,
            error_type=str(event.payload.get("error_type", "model.failed")),
            model_latency_ms_derived=self._take_latency(StepPhase.EXECUTOR, event),
            checkpoint_revision=self._revision,
            status=StepStatus.FAILED,
        )

    def _on_verification(self, event: TraceEvent) -> None:
        rejected = event.type == "verification.rejected"
        self._append(
            phase=StepPhase.EXECUTOR,
            action_kind=StepActionKind.ERROR if rejected else StepActionKind.MODEL,
            error_type="verification_rejected" if rejected else None,
            checkpoint_revision=self._revision,
            status=StepStatus.FAILED if rejected else StepStatus.COMPLETED,
        )

    def _on_finished(self, event: TraceEvent) -> None:
        status = _TERMINAL_STEP_STATUS.get(str(event.payload.get("status")), StepStatus.INCOMPLETE)
        self._final_status = status
        self._append(
            phase=StepPhase.EXECUTOR,
            action_kind=StepActionKind.FINISH,
            error_type=_error_type_of(event.payload),
            checkpoint_revision=self._revision,
            status=status,
        )

    def _take_latency(self, phase: StepPhase, event: TraceEvent) -> float | None:
        """Derive latency from the paired request, then clear the pairing."""
        requested_at = self._pending_request_at.pop(phase.value, None)
        if requested_at is None:
            return None
        delta = (event.timestamp - requested_at).total_seconds() * 1000
        return round(max(0.0, delta), 3)

    def finish(self) -> RunProjection:
        """Freeze the accumulated steps and compute run-level aggregates."""
        incomplete = sum(
            1
            for step in self._steps
            if step.get("action_kind") is StepActionKind.TOOL
            and step.get("status") is StepStatus.INCOMPLETE
        )
        records = tuple(
            StepRecord(step_id=position, **step)
            for position, step in enumerate(self._steps, start=1)
        )
        return RunProjection(
            steps=records,
            tool_sequence=tuple(self._decision_tools),
            tool_arguments=tuple(self._decision_args),
            executed_sequence=tuple(self._executed_tools),
            model_decisions=self._model_decisions,
            tool_attempts=self._attempts,
            plan_events=self._plan_events,
            planner_requests=self._planner_requests,
            model_requests=self._model_requests,
            incomplete_calls=incomplete,
            final_status=self._final_status,
        )


def _attempt_id(call_id: str, attempt: int) -> str:
    """Build the attempt identity; stable today, extended by retries in M10."""
    return f"{call_id}#{attempt}"


def _phase_of(event: TraceEvent) -> StepPhase:
    raw = event.payload.get("phase")
    return StepPhase.PLANNER if raw == StepPhase.PLANNER.value else StepPhase.EXECUTOR


def _budget_of(payload: dict[str, Any]) -> dict[str, int] | None:
    budget = payload.get("budget")
    if not isinstance(budget, dict):
        return None
    return {str(key): _as_int(value) for key, value in budget.items()}


def _error_type_of(payload: dict[str, Any]) -> str | None:
    """Prefer the stable machine-readable code, then the class, then free text.

    A classified tool failure records `error_code`; a model-boundary failure records
    `error_type`. Checking the code first means the projection reports the same
    taxonomy the trace does, rather than falling through to prose.
    """
    error = payload.get("error_code") or payload.get("error_type") or payload.get("error")
    return None if error is None else str(error)


def _as_int(value: Any) -> int:
    """Coerce a trace payload value to an int, defaulting to 0."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


_HANDLERS = {
    "checkpoint.saved": _Builder._on_checkpoint,
    "model.request": _Builder._on_model_request,
    "model.action": _Builder._on_model_action,
    "model.failed": _Builder._on_model_failed,
    "plan.created": _Builder._on_plan_created,
    "plan.failed": _Builder._on_plan_failed,
    "tool.completed": _Builder._on_tool_completed,
    "tool.unknown": _Builder._on_tool_rejected,
    "policy.decided": _Builder._on_policy_decided,
    "run.awaiting_approval": _Builder._on_awaiting_approval,
    "verification.passed": _Builder._on_verification,
    "verification.rejected": _Builder._on_verification,
    "run.finished": _Builder._on_finished,
}

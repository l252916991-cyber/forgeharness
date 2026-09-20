"""Traceable Bad Case records extracted from real benchmark failures.

A Bad Case is not a detached summary: it carries the hash-chained trace that
evidences the failure, the graded expectation, the observed facts, and the
projected steps. Every field needed to re-examine the failure is present, and the
record names the exact `revision`/`profile`/`model`/`sample_id` it came from, so
a number can never float free of its context.

Replay is deliberately split in two, because the two answer different questions:

* ``RECORDED`` replays the model's *boundary behaviour* rather than the model, so
  it can prove harness fixes (parsing, lifecycle, guards) deterministically.
* ``LIVE`` re-runs the real model, which is the only way to show that a prompt or
  planner change altered model behaviour.

Keeping the distinction explicit prevents claiming a model improvement from a
deterministic replay.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from forgeharness.domain.models import (
    AgentAction,
    FinalAction,
    FrozenModel,
    ModelResult,
    ModelUsage,
    ToolAction,
    ToolCall,
)
from forgeharness.observability.hash_chain import HashChainedJSONLTrace, verify_trace
from forgeharness.observability.steps import StepActionKind, project_events


class FailureClass(StrEnum):
    """Why the run failed, decided by evidence rather than by intuition.

    The classes map to the layer a failure lives in, which is what makes a batch
    of Bad Cases diagnostic instead of a pile of symptoms.
    """

    # A recoverable model-boundary violation the harness escalated into a terminal
    # failure. Fixable in the runtime, and the class that shows harness value.
    PROTOCOL_VIOLATION = "protocol_violation"
    # The harness could not consume a response for some other reason.
    HARNESS_PROTOCOL = "harness_protocol"
    # The model's own choices produced the mismatch. Not fixable in the runtime.
    MODEL_QUALITY = "model_quality"
    # The case is unsatisfiable under the profile it was run in.
    CASE_DESIGN = "case_design"


class FailurePoint(StrEnum):
    """The layer that produced the failure."""

    MODEL_ADAPTER = "model_adapter"
    EXECUTION_LOOP = "execution_loop"
    PLANNER = "planner"
    GRADING = "grading"


class ReplayEligibility(StrEnum):
    """Whether a deterministic replay can reproduce this case."""

    DETERMINISTIC = "deterministic"
    LIVE_ONLY = "live_only"


class ReplayMode(StrEnum):
    """What a replay can prove."""

    RECORDED = "recorded"
    LIVE = "live"
    # Declared candidate only: the recorded channel is not implemented yet. This
    # round deliberately stops at capture, so a Bad Case records what *could* be
    # replayed rather than claiming a capability that does not exist.
    RECORDED_MODEL_CANDIDATE = "recorded_model_candidate"


class ReplayError(FrozenModel):
    """A model-boundary failure replayed faithfully instead of as an action.

    Carries the violation's stable ``code`` and whether it was recoverable, so the
    runtime can take the same decision path during replay as it did live without
    the frozen record having to encode runtime semantics itself. ``usage`` is kept
    because a rejected request consumed real tokens.
    """

    error_type: str = Field(min_length=1)
    message: str = ""
    code: str | None = None
    received_tool_calls: int | None = None
    recoverable: bool | None = None
    usage: ModelUsage | None = None


class ReplayStep(FrozenModel):
    """One frozen model outcome: either a validated result or a boundary failure."""

    result: ModelResult | None = None
    error: ReplayError | None = None

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> Self:
        if (self.result is None) == (self.error is None):
            raise ValueError("a replay step sets exactly one of `result` or `error`")
        return self

    @staticmethod
    def from_result(result: ModelResult) -> ReplayStep:
        """Freeze one validated model result."""
        return ReplayStep(result=result)

    @staticmethod
    def from_error(
        error_type: str,
        message: str = "",
        *,
        code: str | None = None,
        received_tool_calls: int | None = None,
        recoverable: bool | None = None,
        usage: ModelUsage | None = None,
    ) -> ReplayStep:
        """Freeze one model-boundary failure with everything needed to rebuild it."""
        return ReplayStep(
            error=ReplayError(
                error_type=error_type,
                message=message,
                code=code,
                received_tool_calls=received_tool_calls,
                recoverable=recoverable,
                usage=usage,
            )
        )


class BadCase(FrozenModel):
    """One reproducible failure, bound to its evidence and its origin."""

    case_id: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    model: str = Field(min_length=1)
    sample_id: int = Field(ge=0)

    # The evidence chain. `trace_path` is relative to the repository root so the
    # record stays portable; `trace_hash` pins the exact bytes.
    trace_path: str = Field(min_length=1)
    trace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    # What the case demanded, what happened, and how it was graded.
    expected_status: str = Field(min_length=1)
    expected_tools: tuple[str, ...] = ()
    expected_sequence: tuple[str, ...] | None = None
    actual_status: str = Field(min_length=1)
    actual_sequence: tuple[str, ...] = ()
    failed_assertions: tuple[str, ...] = ()
    steps: int = Field(ge=0)
    logical_tool_calls: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    projected_steps: tuple[str, ...] = ()

    # Observation counters, recorded separately because they disagree at a
    # rejection boundary. `model_decisions` counts actions the model produced;
    # `model_requests` counts requests the harness issued. A rejected response
    # makes both exceed `usage.steps`, and that gap is evidence, not noise to
    # normalise away.
    model_decisions: int = Field(default=0, ge=0)
    model_requests: int = Field(default=0, ge=0)
    usage_steps: int = Field(default=0, ge=0)
    received_tool_calls: int | None = None

    failure_class: FailureClass
    failure_point: FailurePoint = FailurePoint.GRADING
    error_type: str | None = None
    diagnosis: str = Field(min_length=1)
    # What the harness lacked, when the failure is not purely a model defect.
    harness_gap: str | None = None
    # A contributing weakness in the case itself. Kept separate from the primary
    # class so neither observation is lost by being merged into the other.
    case_design_note: str | None = None

    # Derived diagnostics, computed at capture time so a later fix can be verified
    # against numbers instead of by reading a long trace by eye. The signature and
    # count are populated only when the evidence supports the claim: an identical
    # call returning a *different* observation is not no-progress.
    repeated_calls: tuple[RepeatedCall, ...] = ()
    repeated_call_signature: str | None = None
    repeat_count: int | None = Field(default=None, ge=2)
    observation_hashes: tuple[str, ...] = ()

    replay_eligibility: ReplayEligibility = ReplayEligibility.LIVE_ONLY
    replay_mode: ReplayMode
    replay_note: str = ""
    # None for ReAct; the planner's frozen outcomes for Plan-Execute.
    planner_script: tuple[ReplayStep, ...] | None = None
    executor_script: tuple[ReplayStep, ...] = ()


class BadCaseError(ValueError):
    """Raised when a Bad Case cannot be written or read safely."""


def persist_trace(events: list[object], path: Path, *, task_id: str) -> tuple[str, int]:
    """Write events as a redacted, hash-chained trace; return its hash and size.

    The trace is re-emitted through the hash-chained writer rather than copied, so
    the persisted record is the redacted, tamper-evident form and its chain can be
    verified independently of the Bad Case that points at it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = HashChainedJSONLTrace(path, task_id)
    for event in events:
        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        if event_type is None or payload is None:
            raise BadCaseError("only trace events can be persisted")
        writer.append(str(event_type), dict(payload))
    verification = verify_trace(path)
    if not verification.valid:
        raise BadCaseError(f"persisted trace failed verification: {verification.error}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest, verification.events


def executor_replay_script(events: list[object]) -> tuple[ReplayStep, ...]:
    """Freeze the executor's own model outcomes from a trace.

    Only ``model.action`` and ``model.failed`` feed this. The planner's decision is
    never a ``model.action`` — it is recorded on ``plan.created`` — so the executor
    script cannot contain it and a Plan-Execute replay cannot feed the plan back
    into the executor loop as if the executor model had produced it.
    """
    steps: list[ReplayStep] = []
    for event in events:
        event_type = getattr(event, "type", "")
        payload = getattr(event, "payload", {}) or {}
        if event_type == "model.action":
            action = payload.get("action")
            if not isinstance(action, dict):
                continue
            steps.append(
                ReplayStep.from_result(
                    ModelResult(
                        action=_as_action(action),
                        usage=ModelUsage(
                            input_tokens=_as_int(payload.get("input_tokens")),
                            output_tokens=_as_int(payload.get("output_tokens")),
                        ),
                        model_name=str(payload.get("model_name", "recorded")),
                    )
                )
            )
        elif event_type == "model.failed":
            raw_usage = payload.get("usage")
            usage = ModelUsage.model_validate(raw_usage) if isinstance(raw_usage, dict) else None
            received = payload.get("received_tool_calls")
            recoverable = payload.get("recoverable")
            code = payload.get("error_code")
            steps.append(
                ReplayStep.from_error(
                    str(payload.get("error_type", "RuntimeError")),
                    str(payload.get("error", "")),
                    code=str(code) if code is not None else None,
                    received_tool_calls=received if isinstance(received, int) else None,
                    recoverable=recoverable if isinstance(recoverable, bool) else None,
                    usage=usage,
                )
            )
    return tuple(steps)


def planner_replay_script(events: list[object]) -> tuple[ReplayStep, ...]:
    """Freeze the planner's own responses from a trace.

    A rejected attempt is frozen as the *response the model produced* (recorded on
    ``plan.failed``), not as the resulting exception. That distinction is what
    makes the record useful: replaying an exception can only reproduce the
    failure, whereas replaying the response lets a corrected runtime re-judge it —
    which is how a planner-protocol fix can be shown to work.

    ``plan.created`` records only the accepted plan, so it is reconstructed from
    the validated steps.
    """
    steps: list[ReplayStep] = []
    for event in events:
        event_type = getattr(event, "type", "")
        payload = getattr(event, "payload", {}) or {}
        if event_type == "plan.created":
            steps_text = payload.get("steps")
            content = json.dumps({"steps": list(steps_text)}) if steps_text else ""
            steps.append(
                ReplayStep.from_result(
                    ModelResult(
                        action=FinalAction(content=content),
                        usage=_recorded_usage(payload),
                        model_name=str(payload.get("model_name", "recorded-planner")),
                    )
                )
            )
        elif event_type == "plan.failed":
            recorded = payload.get("response")
            if isinstance(recorded, dict):
                steps.append(_replay_step_from_response(recorded))
            else:
                # A trace from before responses were recorded: the failure is
                # reproducible, but a corrected runtime cannot be exercised on it.
                steps.append(
                    ReplayStep.from_error("PlanProtocolViolation", str(payload.get("error", "")))
                )
    return tuple(steps)


def _replay_step_from_response(recorded: dict[str, object]) -> ReplayStep:
    """Rebuild a planner outcome from a recorded response payload."""
    usage = _recorded_usage(recorded)
    model_name = str(recorded.get("model_name", "recorded-planner"))
    if recorded.get("kind") == "tool":
        raw_args = recorded.get("arguments")
        arguments: dict[str, object] = dict(raw_args) if isinstance(raw_args, dict) else {}
        return ReplayStep.from_result(
            ModelResult(
                action=ToolAction(
                    call=ToolCall(
                        id="recorded-planner-call",
                        name=str(recorded.get("tool", "unknown")),
                        arguments=arguments,
                    )
                ),
                usage=usage,
                model_name=model_name,
            )
        )
    return ReplayStep.from_result(
        ModelResult(
            action=FinalAction(content=str(recorded.get("content", ""))),
            usage=usage,
            model_name=model_name,
        )
    )


def _recorded_usage(payload: dict[str, object]) -> ModelUsage:
    """Read recorded token usage from a payload, defaulting to zero."""
    return ModelUsage(
        input_tokens=_as_int(payload.get("input_tokens")),
        output_tokens=_as_int(payload.get("output_tokens")),
    )


class RepeatedCall(FrozenModel):
    """One tool call signature observed more than once in a run."""

    signature: str = Field(min_length=1)
    repeat_count: int = Field(ge=2)
    observation_hashes: tuple[str, ...]
    all_observations_identical: bool


def no_progress_diagnosis(events: list[object]) -> tuple[tuple[RepeatedCall, ...], str | None]:
    """Detect repeated calls that obtained no new information.

    A repeat is only called no-progress when the evidence supports it: identical
    tool name, identical canonicalised arguments, and an identical observation for
    every repeat. Repeating a call with *different* results may be legitimate
    progress (a file changed, a retry succeeded), so identical arguments alone are
    never enough. Returns the detected repeats and a derived signature summary.
    """
    projection = project_events(events)  # type: ignore[arg-type]
    grouped: dict[str, list[str]] = {}
    for step in projection.steps:
        if step.action_kind is not StepActionKind.TOOL or not step.tool_name:
            continue
        if step.observation is None:
            continue
        canonical = json.dumps(step.tool_args or {}, sort_keys=True, separators=(",", ":"))
        signature = f"{step.tool_name}:{canonical}"
        digest = hashlib.sha256(step.observation.encode()).hexdigest()
        grouped.setdefault(signature, []).append(digest)
    repeats: list[RepeatedCall] = []
    for signature, hashes in grouped.items():
        if len(hashes) < 2:
            continue
        repeats.append(
            RepeatedCall(
                signature=signature,
                repeat_count=len(hashes),
                observation_hashes=tuple(hashes),
                all_observations_identical=len(set(hashes)) == 1,
            )
        )
    repeats.sort(key=lambda item: (-item.repeat_count, item.signature))
    for repeat in repeats:
        if repeat.all_observations_identical:
            return tuple(repeats), repeat.signature
    return tuple(repeats), None


def _as_action(payload: dict[str, object]) -> AgentAction:
    from forgeharness.domain.models import FinalAction, ToolAction, ToolCall

    if payload.get("kind") == "tool":
        call = payload.get("call")
        if not isinstance(call, dict):
            raise BadCaseError("tool action payload has no call")
        return ToolAction(
            call=ToolCall(
                id=str(call.get("id", "")),
                name=str(call.get("name", "")),
                arguments=dict(call.get("arguments") or {}),
            )
        )
    return FinalAction(content=str(payload.get("content", "")))


def _as_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


class FailureEvidence(FrozenModel):
    """The boundary facts a classification is derived from."""

    actual_status: str
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    received_tool_calls: int | None = None
    failed_assertions: tuple[str, ...] = ()
    # Call-budget facts, so a completed run that overshot its contract can be
    # described causally instead of by echoing the assertion text.
    expected_max_tool_calls: int | None = None
    actual_tool_calls: int | None = None


def classify_failure(
    evidence: FailureEvidence,
) -> tuple[FailureClass, FailurePoint, str, str | None]:
    """Classify a failure from evidence, returning class, point, diagnosis and gap.

    The rule is structural rather than a per-case table, so it generalises to
    failures nobody anticipated:

    * a recoverable model-boundary violation the harness escalated into a terminal
      failure is ``protocol_violation`` / ``model_adapter`` — the case that shows
      harness value, because the model misbehaved but the runtime chose to give up;
    * any other model-boundary termination is ``harness_protocol``;
    * a run that completed but mismatched its contract is ``model_quality``.
    """
    if evidence.error_code == "multiple_tool_calls_not_allowed":
        count = evidence.received_tool_calls
        received = f"{count} tool calls" if count is not None else "multiple tool calls"
        return (
            FailureClass.PROTOCOL_VIOLATION,
            FailurePoint.MODEL_ADAPTER,
            (
                f"Model returned {received} despite parallel_tool_calls=False; the adapter "
                "escalated this recoverable model-protocol violation into a terminal run "
                "failure before Runtime admission or tool dispatch."
            ),
            (
                "the harness had no path from a rejected multi-call response back into the "
                "loop: it neither admitted nor dispatched any call, and gave the model no "
                "chance to correct itself"
            ),
        )
    if evidence.error_type == "plan.failed":
        detail = evidence.error_message or "the planner returned no usable plan"
        return (
            FailureClass.HARNESS_PROTOCOL,
            FailurePoint.PLANNER,
            (
                f"the planning phase produced no reusable plan ({detail}), so the run "
                "ended before the executor could act"
            ),
            (
                "a rejected plan ends the run instead of returning the planner to the "
                "model with a correction opportunity"
            ),
        )
    if evidence.error_code is not None or evidence.error_type is not None:
        detail = evidence.error_message or evidence.error_type or "unknown error"
        return (
            FailureClass.HARNESS_PROTOCOL,
            FailurePoint.MODEL_ADAPTER,
            f"the run terminated at the model boundary ({detail})",
            "the harness had no path from a rejected model response back into the loop",
        )
    if evidence.actual_status == "failed":
        return (
            FailureClass.HARNESS_PROTOCOL,
            FailurePoint.EXECUTION_LOOP,
            "the run failed without a recorded model-boundary error",
            "the failure reason was not observable at the model boundary",
        )
    detail = ", ".join(evidence.failed_assertions) if evidence.failed_assertions else "no detail"
    overshoot: str | None = None
    if (
        evidence.expected_max_tool_calls is not None
        and evidence.actual_tool_calls is not None
        and evidence.actual_tool_calls > evidence.expected_max_tool_calls
    ):
        overshoot = (
            f"the model executed {evidence.actual_tool_calls} tool calls against a "
            f"contract of at most {evidence.expected_max_tool_calls}"
        )
    return (
        FailureClass.MODEL_QUALITY,
        FailurePoint.GRADING,
        (
            f"{overshoot}; its choices diverged from the stated sequence ({detail})"
            if overshoot
            else f"the run completed but did not satisfy its contract ({detail})"
        ),
        None,
    )


def fixture_gap_note(fixture_paths: tuple[str, ...], events: list[object]) -> str | None:
    """Note when a case's own fixture points at a location it does not provide.

    A fixture whose text references a path that does not exist invites an agent to
    keep searching for it, so the resulting over-exploration is partly a case
    weakness rather than purely a model defect. Only a failed call whose target is
    genuinely absent from the fixture counts; a call that succeeded proves the
    location exists.
    """
    provided = {_normalise(path) for path in fixture_paths}
    for step in project_events(events).steps:  # type: ignore[arg-type]
        if step.action_kind is not StepActionKind.TOOL or step.tool_ok is not False:
            continue
        target = _normalise(str((step.tool_args or {}).get("path", "")))
        if not target or target == ".":
            continue
        if not _covered_by(target, provided):
            return (
                f"the model probed {target!r}, which the fixture does not provide, so the "
                "case's own content invited exploration the contract does not allow for"
            )
    return None


def _normalise(path: str) -> str:
    """Strip a leading './' so fixture and call paths compare directly."""
    return path[2:] if path.startswith("./") else path


def _covered_by(target: str, provided: set[str]) -> bool:
    """True when a fixture file lives at or below `target`."""
    return any(item == target or item.startswith(target + "/") for item in provided)


def projected_step_summary(events: list[object]) -> tuple[str, ...]:
    """Render projected steps as compact strings for the Bad Case record."""
    projection = project_events(events)  # type: ignore[arg-type]
    return tuple(
        f"{step.step_id}:{step.phase.value}/{step.action_kind.value}:{step.tool_name or '-'}"
        f":{step.status.value}"
        for step in projection.steps
    )


def write_bad_case(bad_case: BadCase, path: Path) -> None:
    """Persist one Bad Case as deterministic JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bad_case.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_bad_case(path: Path) -> BadCase:
    """Read one Bad Case record."""
    return BadCase.model_validate_json(path.read_text(encoding="utf-8"))

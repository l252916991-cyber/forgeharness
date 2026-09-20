"""Bad Case classification, artifact identity, and progress diagnostics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from forgeharness.domain.models import FinalAction, ModelResult, ModelUsage, TraceEvent
from forgeharness.evaluation.agent import Profile, Strategy, bad_case_artifact_key
from forgeharness.evaluation.bad_case import (
    BadCase,
    FailureClass,
    FailureEvidence,
    FailurePoint,
    ReplayError,
    classify_failure,
    fixture_gap_note,
    no_progress_diagnosis,
)


class _NoToolModel:
    """A deterministic model that never calls a tool, standing in for a live one."""

    async def decide(self, request: object) -> ModelResult:
        tools = getattr(request, "tools", ())
        content = json.dumps({"steps": ["answer directly"]}) if not tools else "no tool needed"
        return ModelResult(
            action=FinalAction(content=content),
            usage=ModelUsage(input_tokens=10, output_tokens=3),
        )


def _no_tool_model() -> _NoToolModel:
    return _NoToolModel()


def _call_events(
    sequence: int,
    *,
    call_id: str,
    tool: str,
    arguments: dict[str, object],
    ok: bool,
    observation: str,
) -> list[TraceEvent]:
    """One model decision plus its execution, as a real trace records them.

    Arguments live on the decision, not on the result, so a synthetic trace needs
    both events: the projection sources a tool step's args from the ``model.action``
    that requested it.
    """
    decision = TraceEvent(
        task_id="case-1",
        sequence=sequence,
        type="model.action",
        payload={
            "kind": "tool",
            "action": {
                "kind": "tool",
                "call": {"id": call_id, "name": tool, "arguments": arguments},
            },
            "input_tokens": 1,
            "output_tokens": 1,
        },
    )
    execution = TraceEvent(
        task_id="case-1",
        sequence=sequence + 1,
        type="tool.completed",
        payload={
            "call_id": call_id,
            "tool": tool,
            "ok": ok,
            "elapsed_ms": 1,
            "observation": observation,
            "metadata": {},
        },
    )
    return [decision, execution]


def _trace(*calls: dict[str, object]) -> list[TraceEvent]:
    """Build a trace from call specifications, numbering events in order."""
    events: list[TraceEvent] = []
    for index, call in enumerate(calls):
        events.extend(_call_events(2 * index + 1, **call))  # type: ignore[arg-type]
    return events


def _search(
    call_id: str, query: str, *, ok: bool = True, observation: str = "A"
) -> dict[str, object]:
    return {
        "call_id": call_id,
        "tool": "search_code",
        "arguments": {"query": query},
        "ok": ok,
        "observation": observation,
    }


def test_no_progress_requires_identical_args_and_identical_observations() -> None:
    """Repeating a call with *different* results is not no-progress."""
    events = _trace(_search("c1", "x", observation="A"), _search("c2", "x", observation="B"))
    repeats, signature = no_progress_diagnosis(events)
    assert signature is None
    assert len(repeats) == 1
    assert repeats[0].repeat_count == 2
    assert repeats[0].all_observations_identical is False


def test_identical_args_and_observations_are_flagged_as_no_progress() -> None:
    events = _trace(_search("c1", "x"), _search("c2", "x"), _search("c3", "x"))
    repeats, signature = no_progress_diagnosis(events)
    assert signature == 'search_code:{"query":"x"}'
    assert repeats[0].repeat_count == 3
    assert repeats[0].all_observations_identical is True
    assert repeats[0].observation_hashes == (hashlib.sha256(b"A").hexdigest(),) * 3


def test_different_arguments_are_never_a_repeat() -> None:
    """Guessing new queries is not a repeated call, even when it keeps failing."""
    events = _trace(
        _search("c1", "a", ok=False, observation="boom"),
        _search("c2", "b", ok=False, observation="boom"),
    )
    repeats, signature = no_progress_diagnosis(events)
    assert repeats == ()
    assert signature is None


def test_single_call_is_not_a_repeat() -> None:
    repeats, signature = no_progress_diagnosis(_trace(_search("c1", "x")))
    assert repeats == ()
    assert signature is None


def test_fixture_gap_notes_a_failed_probe_of_a_missing_location() -> None:
    events = _trace(
        {
            "call_id": "c1",
            "tool": "search_code",
            "arguments": {"query": "retry", "path": "./runtime"},
            "ok": False,
            "observation": "tool execution failed: FileNotFoundError",
        }
    )
    note = fixture_gap_note(("README.md",), events)
    assert note is not None
    assert "runtime" in note


def test_fixture_gap_is_silent_when_the_probe_succeeded() -> None:
    """A successful probe proves the location exists, so there is no case gap."""
    events = _trace(
        {
            "call_id": "c1",
            "tool": "search_code",
            "arguments": {"query": "retry", "path": "./runtime"},
            "ok": True,
            "observation": "no matches",
        }
    )
    assert fixture_gap_note(("runtime/loop.py",), events) is None


def test_fixture_gap_ignores_failures_inside_the_fixture() -> None:
    events = _trace(
        {
            "call_id": "c1",
            "tool": "read_file",
            "arguments": {"path": "pkg/util.py"},
            "ok": False,
            "observation": "not a regular file",
        }
    )
    assert fixture_gap_note(("pkg/util.py",), events) is None


def test_classification_is_driven_by_the_error_code_not_the_message() -> None:
    """Rewording the provider message must not change the classification."""
    evidence = FailureEvidence(
        actual_status="failed",
        error_code="multiple_tool_calls_not_allowed",
        error_type="ModelProtocolError",
        error_message="totally different wording",
        received_tool_calls=3,
    )
    failure_class, point, diagnosis, gap = classify_failure(evidence)
    assert failure_class is FailureClass.PROTOCOL_VIOLATION
    assert point is FailurePoint.MODEL_ADAPTER
    assert "3 tool calls" in diagnosis
    assert gap is not None


def test_other_boundary_errors_are_harness_protocol() -> None:
    evidence = FailureEvidence(
        actual_status="failed",
        error_type="ModelProtocolError",
        error_message="completion contained neither content nor a tool call",
    )
    failure_class, point, _, _ = classify_failure(evidence)
    assert failure_class is FailureClass.HARNESS_PROTOCOL
    assert point is FailurePoint.MODEL_ADAPTER


def test_model_quality_diagnosis_states_the_overshoot_causally() -> None:
    evidence = FailureEvidence(
        actual_status="succeeded",
        failed_assertions=("tool_calls:5>max:2",),
        expected_max_tool_calls=2,
        actual_tool_calls=5,
    )
    failure_class, point, diagnosis, gap = classify_failure(evidence)
    assert failure_class is FailureClass.MODEL_QUALITY
    assert point is FailurePoint.GRADING
    assert "5 tool calls against a contract of at most 2" in diagnosis
    assert gap is None


def test_model_quality_without_a_call_ceiling_uses_the_assertion_detail() -> None:
    evidence = FailureEvidence(
        actual_status="succeeded",
        failed_assertions=("missing_tools:write_file",),
    )
    _, _, diagnosis, _ = classify_failure(evidence)
    assert "missing_tools:write_file" in diagnosis


def test_artifact_key_separates_profile_and_sample() -> None:
    """The same arm+case in another profile or sample must not overwrite evidence."""
    keys = {
        bad_case_artifact_key(
            profile=profile, strategy=Strategy.PLAN_EXECUTE, case_id="c", sample_id=sample
        )
        for profile in (Profile.EQUAL, Profile.NATURAL)
        for sample in (0, 1)
    }
    assert keys == {
        "equal-plan_execute-c-s0",
        "equal-plan_execute-c-s1",
        "natural-plan_execute-c-s0",
        "natural-plan_execute-c-s1",
    }


def test_replay_step_requires_exactly_one_outcome() -> None:
    """A frozen step is either a result or a failure, never both or neither."""
    from pydantic import ValidationError

    from forgeharness.evaluation.bad_case import ReplayStep

    with pytest.raises(ValidationError):
        ReplayStep()
    with pytest.raises(ValidationError):
        ReplayStep(
            result=ModelResult(action=FinalAction(content="x")),
            error=ReplayError(error_type="E"),
        )


def test_executor_script_excludes_planner_and_keeps_failures() -> None:
    """The executor script is built from `model.action` only."""
    from forgeharness.evaluation.bad_case import executor_replay_script

    events = [
        TraceEvent(
            task_id="c",
            sequence=1,
            type="model.action",
            payload={"kind": "final", "action": {"kind": "final", "content": "done"}},
        ),
        TraceEvent(
            task_id="c",
            sequence=2,
            type="model.failed",
            payload={"error_type": "ModelProtocolError", "error": "boom"},
        ),
    ]
    script = executor_replay_script(events)
    assert len(script) == 2
    assert script[1].error is not None
    assert script[1].error.error_type == "ModelProtocolError"


def test_planner_script_is_built_from_plan_events_only() -> None:
    """The planner's outcome lives on `plan.created`, never on `model.action`."""
    from forgeharness.evaluation.bad_case import planner_replay_script

    events = [
        TraceEvent(
            task_id="c",
            sequence=1,
            type="model.action",
            payload={"kind": "final", "action": {"kind": "final", "content": "executor answer"}},
        ),
        TraceEvent(
            task_id="c",
            sequence=2,
            type="plan.created",
            payload={"steps": ["one", "two"], "input_tokens": 5, "output_tokens": 2},
        ),
        TraceEvent(
            task_id="c",
            sequence=3,
            type="plan.failed",
            payload={"error": "planner returned an invalid plan"},
        ),
    ]
    script = planner_replay_script(events)
    assert len(script) == 2
    planned = script[0].result
    assert planned is not None
    assert planned.action.kind == "final"
    assert '"steps": ["one", "two"]' in planned.action.content
    assert script[1].error is not None
    # The executor's own answer must never appear in the planner script.
    assert all(
        step.result is None or "executor answer" not in step.result.action.model_dump_json()
        for step in script
    )


def test_planner_script_is_empty_without_plan_events() -> None:
    from forgeharness.evaluation.bad_case import planner_replay_script

    events = [
        TraceEvent(
            task_id="c",
            sequence=1,
            type="model.action",
            payload={"kind": "final", "action": {"kind": "final", "content": "x"}},
        )
    ]
    assert planner_replay_script(events) == ()


def test_empty_tool_observation_is_ignored_by_progress_detection() -> None:
    """A step without an observation cannot be compared, so it is skipped."""
    events = _trace(
        _search("c1", "x", observation="A"),
        _search("c2", "x", observation="A"),
    )
    # Blank the second observation: the pairing is then unverifiable.
    events[3] = events[3].model_copy(update={"payload": {**events[3].payload, "observation": None}})
    repeats, signature = no_progress_diagnosis(events)
    assert repeats == ()
    assert signature is None


def test_fixture_gap_treats_root_paths_as_uninformative() -> None:
    """`.` and an absent path say nothing about the fixture, so no note is made."""
    for path in (".", ""):
        events = _trace(
            {
                "call_id": "c1",
                "tool": "list_files",
                "arguments": {"path": path},
                "ok": False,
                "observation": "not a directory",
            }
        )
        assert fixture_gap_note(("README.md",), events) is None


def test_failed_run_without_a_boundary_error_is_harness_protocol() -> None:
    """A `failed` run with no recorded boundary error is unreached observability."""
    evidence = FailureEvidence(actual_status="failed")
    failure_class, point, _, gap = classify_failure(evidence)
    assert failure_class is FailureClass.HARNESS_PROTOCOL
    assert point is FailurePoint.EXECUTION_LOOP
    assert gap is not None


def test_coercion_handles_json_scalars() -> None:
    """Trace payload numbers arrive as JSON scalars and must coerce safely."""
    from forgeharness.evaluation.bad_case import _as_int

    assert _as_int(5) == 5
    assert _as_int(5.9) == 5
    assert _as_int(True) == 0
    assert _as_int(None) == 0
    assert _as_int("12") == 0


def test_malformed_action_payloads_are_rejected() -> None:
    """A trace whose action payloads are unreadable must not be silently skipped."""
    from forgeharness.evaluation.bad_case import BadCaseError, executor_replay_script

    with pytest.raises(BadCaseError, match="no call"):
        executor_replay_script(
            [
                TraceEvent(
                    task_id="c",
                    sequence=1,
                    type="model.action",
                    payload={"kind": "tool", "action": {"kind": "tool"}},
                )
            ]
        )


def test_non_event_input_is_rejected_by_persist_trace(tmp_path: Path) -> None:
    """Persisting requires real trace events, not arbitrary objects."""
    from forgeharness.evaluation.bad_case import BadCaseError, persist_trace

    with pytest.raises(BadCaseError, match="only trace events"):
        persist_trace([object()], tmp_path / "t.jsonl", task_id="c")


def test_planner_rejection_points_at_the_planner_layer() -> None:
    """A rejected plan belongs to the planner, not to the generic model adapter.

    The layer matters diagnostically: a planner that cannot produce a usable plan
    is a different fix from the executor loop mishandling a response.
    """
    for message in (
        "planner returned an invalid plan: planner response contains no JSON object",
        "planner response is not valid JSON: Expecting ',' delimiter",
    ):
        evidence = FailureEvidence(
            actual_status="failed",
            error_type="plan.failed",
            error_message=message,
        )
        failure_class, point, diagnosis, gap = classify_failure(evidence)
        assert failure_class is FailureClass.HARNESS_PROTOCOL
        assert point is FailurePoint.PLANNER
        assert "planning phase" in diagnosis
        assert gap is not None


async def test_pure_model_quality_failures_are_not_captured(tmp_path: Path) -> None:
    """A failure with nothing to fix must not become a Bad Case.

    A full regression produces ~30 model-quality failures; capturing them would
    bury the handful that need harness work, and each is already recorded in the
    report.
    """
    from forgeharness.evaluation.agent import run_agent_evaluation

    manifest_path = Path(__file__).parents[2] / "evals" / "agent_cases.json"
    project_root = Path(__file__).parents[2]
    report = await run_agent_evaluation(
        manifest_path=manifest_path,
        output_path=tmp_path / "agent-eval.json",
        project_root=project_root,
        split="dev",
        profile=Profile.EQUAL,
        model_factory=_no_tool_model,
        samples=2,
    )
    captured = (
        sorted((tmp_path / "bad-cases").glob("*.json")) if (tmp_path / "bad-cases").exists() else []
    )
    # The run definitely failed cases (the fake model never calls a tool)...
    assert any(arm.benchmark_pass < arm.benchmark_cases for arm in report.arms)
    # ...yet every captured record has something for the harness to act on.
    for path in captured:
        record = BadCase.model_validate_json(path.read_text(encoding="utf-8"))
        assert not (
            record.failure_class is FailureClass.MODEL_QUALITY
            and record.harness_gap is None
            and record.case_design_note is None
        )


def test_planner_script_reads_a_recorded_response_verbatim() -> None:
    """A rejected planner response is frozen as the model's own output."""
    from forgeharness.evaluation.bad_case import planner_replay_script

    xml = "<start_marker>Reading app.py...</start_marker>"
    script = planner_replay_script(
        [
            TraceEvent(
                task_id="c",
                sequence=1,
                type="plan.failed",
                payload={
                    "error_code": "missing_plan_json",
                    "response": {
                        "kind": "final",
                        "content": xml,
                        "input_tokens": 7,
                        "output_tokens": 3,
                    },
                },
            )
        ]
    )
    assert len(script) == 1
    result = script[0].result
    assert result is not None
    assert result.action.kind == "final"
    # The verbatim content is what a corrected runtime must be able to re-judge.
    assert result.action.content == xml
    assert result.usage.input_tokens == 7


def test_planner_script_reads_a_recorded_tool_call() -> None:
    """A planner that answered with a tool call is frozen as that call."""
    from forgeharness.evaluation.bad_case import planner_replay_script

    script = planner_replay_script(
        [
            TraceEvent(
                task_id="c",
                sequence=1,
                type="plan.failed",
                payload={
                    "error_code": "planner_called_tool",
                    "response": {
                        "kind": "tool",
                        "tool": "echo",
                        "arguments": {"text": "hi"},
                        "input_tokens": 5,
                        "output_tokens": 2,
                    },
                },
            )
        ]
    )
    result = script[0].result
    assert result is not None
    assert result.action.kind == "tool"
    assert result.action.call.name == "echo"
    assert result.action.call.arguments == {"text": "hi"}


def test_planner_script_falls_back_when_no_response_was_recorded() -> None:
    """An older trace cannot exercise a fix, but must still reproduce the failure."""
    from forgeharness.evaluation.bad_case import planner_replay_script

    script = planner_replay_script(
        [
            TraceEvent(
                task_id="c",
                sequence=1,
                type="plan.failed",
                payload={"error": "planner returned an invalid plan: no JSON object"},
            )
        ]
    )
    assert len(script) == 1
    assert script[0].error is not None
    assert script[0].error.error_type == "PlanProtocolViolation"


def test_planner_script_ignores_a_non_dict_response() -> None:
    """A malformed recorded payload degrades to the failure-only form."""
    from forgeharness.evaluation.bad_case import planner_replay_script

    script = planner_replay_script(
        [
            TraceEvent(
                task_id="c",
                sequence=1,
                type="plan.failed",
                payload={"error": "bad", "response": "not-a-dict"},
            )
        ]
    )
    assert script[0].error is not None


def test_malformed_action_payload_in_executor_script_is_rejected() -> None:
    """An unreadable action payload must be surfaced, not skipped silently."""
    from forgeharness.evaluation.bad_case import BadCaseError, executor_replay_script

    with pytest.raises(BadCaseError, match="no call"):
        executor_replay_script(
            [
                TraceEvent(
                    task_id="c",
                    sequence=1,
                    type="model.action",
                    payload={"kind": "tool", "action": {"kind": "tool"}},
                )
            ]
        )


def test_a_recovered_run_is_not_classified_as_a_harness_failure() -> None:
    """A violation that was corrected must not be reported as the cause of failure.

    Once protocol violations can be retried, a `plan.failed` or `model.failed`
    event may be followed by a successful run. Attributing that run to the
    boundary failure would mislabel a recovered execution as a harness defect and
    hide the real (grading) reason.
    """
    from forgeharness.evaluation.agent import _model_boundary_evidence

    events = [
        TraceEvent(
            task_id="c",
            sequence=1,
            type="plan.failed",
            payload={"error_code": "invalid_plan_json", "error": "bad json", "recoverable": True},
        ),
        TraceEvent(
            task_id="c",
            sequence=2,
            type="plan.created",
            payload={"steps": ["one"]},
        ),
        TraceEvent(task_id="c", sequence=3, type="run.finished", payload={"status": "succeeded"}),
    ]

    # The run succeeded: the violation was recovered, so it is not the cause.
    recovered = _model_boundary_evidence(events, "succeeded")
    assert recovered.error_code is None
    assert recovered.actual_status == "succeeded"
    failure_class, _, diagnosis, _ = classify_failure(
        recovered.model_copy(update={"failed_assertions": ("missing_tools:echo",)})
    )
    assert failure_class is FailureClass.MODEL_QUALITY
    assert "missing_tools:echo" in diagnosis

    # The same events on a genuinely failed run are a real harness failure.
    failed = _model_boundary_evidence(events, "failed")
    assert failed.error_code == "invalid_plan_json"
    assert failed.actual_status == "failed"


def test_a_failed_run_reports_the_boundary_cause() -> None:
    from forgeharness.evaluation.agent import _model_boundary_evidence

    events = [
        TraceEvent(
            task_id="c",
            sequence=1,
            type="model.failed",
            payload={
                "error_code": "multiple_tool_calls_not_allowed",
                "error_type": "ModelProtocolError",
                "error": "two calls",
                "received_tool_calls": 2,
            },
        ),
        TraceEvent(task_id="c", sequence=2, type="run.finished", payload={"status": "failed"}),
    ]
    evidence = _model_boundary_evidence(events, "failed")
    failure_class, point, _, _ = classify_failure(evidence)
    assert evidence.received_tool_calls == 2
    assert failure_class is FailureClass.PROTOCOL_VIOLATION
    assert point is FailurePoint.MODEL_ADAPTER

"""Agent Benchmark schema invariants, scorer correctness, and manifest gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolCall,
)
from forgeharness.evaluation.agent import (
    BENCHMARK_TOOL_NAMES,
    BENCHMARK_VERSION,
    BUDGET_PROFILE_VERSION,
    EQUAL_TOTAL_RUN_STEPS,
    KEYLESS_ONLY_CATEGORIES,
    LIVE_MAX_OUTPUT_TOKENS,
    NATURAL_MAX_STEPS,
    OBSERVATION_CONTRACT_VERSION,
    PLANNER_STEPS,
    RECOVERY_SEMANTICS_VERSION,
    AgentCase,
    AgentCaseManifest,
    AgentEvaluationReport,
    CaseCategory,
    CaseOutcome,
    ObservedRun,
    Profile,
    Strategy,
    constraint_binding,
    contract_checks,
    executor_step_budget,
    judge_case,
    live_budget,
    live_eval_config,
    natural_cost_budget,
    run_agent_evaluation,
    whole_run_budget,
)
from forgeharness.evaluation.bad_case import (
    BadCase,
)
from forgeharness.models.base import Model, ModelProtocolError, ModelRequest
from forgeharness.observability.hash_chain import verify_trace

PROJECT_ROOT = Path(__file__).parents[2]
MANIFEST_PATH = PROJECT_ROOT / "evals" / "agent_cases.json"


def _case(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "sample-case",
        "category": "single_tool",
        "split": "dev",
        "task": "Echo a marker.",
        "tools": ["echo"],
        "script": [{"tool": "echo", "arguments": {"text": "a"}}, {"final": "done"}],
        "expected": {"status": "succeeded", "tools": ["echo"]},
    }
    base.update(overrides)
    return base


def _observed(**overrides: object) -> ObservedRun:
    base: dict[str, object] = {
        "status": RunStatus.SUCCEEDED,
        "tool_sequence": ("echo",),
        "tool_arguments": ({"text": "a"},),
        "plan_events": 0,
        "planner_requests": 0,
        "steps": 2,
        "model_decisions": 1,
        "logical_tool_calls": 1,
        "tool_attempts": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "latency_ms": 1.0,
    }
    base.update(overrides)
    return ObservedRun(**base)  # type: ignore[arg-type]


def _manifest() -> AgentCaseManifest:
    return AgentCaseManifest.model_validate_json(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_script_step_requires_exactly_one_decision() -> None:
    with pytest.raises(ValidationError):
        AgentCase.model_validate(
            _case(script=[{"tool": "echo", "arguments": {"text": "a"}, "final": "both"}])
        )
    with pytest.raises(ValidationError):
        AgentCase.model_validate(_case(script=[{}]))


def test_expected_tools_must_be_available_and_known() -> None:
    with pytest.raises(ValidationError, match="not available"):
        AgentCase.model_validate(_case(expected={"status": "succeeded", "tools": ["read_file"]}))
    with pytest.raises(ValidationError, match="unknown benchmark tools"):
        AgentCase.model_validate(_case(tools=["echo", "rm_rf"]))


def test_no_tool_case_cannot_require_tools() -> None:
    with pytest.raises(ValidationError, match="must not require tools"):
        AgentCase.model_validate(
            _case(expected={"status": "succeeded", "no_tool": True, "tools": ["echo"]})
        )


def test_argument_constraints_require_aligned_sequence() -> None:
    with pytest.raises(ValidationError, match="requires tool_sequence"):
        AgentCase.model_validate(
            _case(expected={"status": "succeeded", "args_contains": [{"text": "a"}]})
        )
    with pytest.raises(ValidationError, match="as long as tool_sequence"):
        AgentCase.model_validate(
            _case(
                expected={
                    "status": "succeeded",
                    "tool_sequence": ["echo", "echo"],
                    "args_contains": [{"text": "a"}],
                }
            )
        )


def test_probe_property_follows_grade() -> None:
    assert not AgentCase.model_validate(_case()).is_probe
    assert AgentCase.model_validate(_case(grade="fail", expected={"status": "succeeded"})).is_probe


def test_manifest_requires_unique_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        AgentCaseManifest.model_validate(
            {"schema_version": 1, "min_holdout": 1, "cases": [_case(), _case()]}
        )


def test_manifest_enforces_holdout_floor() -> None:
    with pytest.raises(ValidationError, match="holdout split needs at least"):
        AgentCaseManifest.model_validate(
            {"schema_version": 1, "min_holdout": 2, "cases": [_case()]}
        )


def test_manifest_enforces_category_coverage() -> None:
    with pytest.raises(ValidationError, match="categories without coverage"):
        AgentCaseManifest.model_validate(
            {
                "schema_version": 1,
                "min_holdout": 1,
                "cases": [_case(split="holdout")],
            }
        )


def test_checked_in_manifest_is_valid_and_covers_every_category() -> None:
    manifest = _manifest()
    assert {case.category for case in manifest.cases} == set(CaseCategory)
    assert len({case.id for case in manifest.cases}) == len(manifest.cases)
    assert len(manifest.for_split("holdout")) >= manifest.min_holdout
    for case in manifest.cases:
        assert set(case.tools) <= set(BENCHMARK_TOOL_NAMES)
    assert sum(case.is_probe for case in manifest.cases) >= 5


def test_probes_are_dev_only_and_benchmark_cases_cover_both_splits() -> None:
    """Probes exist to test the scorer, so they must never occupy holdout slots."""
    manifest = _manifest()
    assert all(case.split == "dev" for case in manifest.cases if case.is_probe)
    holdout = manifest.for_split("holdout")
    assert holdout and all(not case.is_probe for case in holdout)
    dev_benchmark = [case for case in manifest.for_split("dev") if not case.is_probe]
    assert dev_benchmark


def test_judge_passes_on_contract_satisfying_run() -> None:
    case = AgentCase.model_validate(
        _case(
            expected={
                "status": "succeeded",
                "tools": ["echo"],
                "tool_sequence": ["echo"],
                "args_contains": [{"text": "a"}],
            }
        )
    )
    verdict = judge_case(case, _observed())
    assert verdict.passed
    assert verdict.failed_assertions == ()
    assert verdict.metrics.tool_set_accuracy == 1.0
    assert verdict.metrics.argument_accuracy == 1.0


def test_judge_detects_missing_tool() -> None:
    case = AgentCase.model_validate(
        _case(
            tools=["echo", "read_file"],
            script=[{"tool": "echo", "arguments": {"text": "a"}}, {"final": "done"}],
            expected={"status": "succeeded", "tools": ["read_file"]},
        )
    )
    verdict = judge_case(case, _observed())
    assert not verdict.passed
    assert any(item.startswith("missing_tools:") for item in verdict.failed_assertions)


def test_judge_detects_forbidden_tool() -> None:
    case = AgentCase.model_validate(
        _case(expected={"status": "succeeded", "forbidden_tools": ["echo"]})
    )
    verdict = judge_case(case, _observed())
    assert not verdict.passed
    assert verdict.metrics.forbidden_violation


def test_judge_detects_wrong_order_with_identical_tool_set() -> None:
    """Set membership is not enough: the sequence must match."""
    case = AgentCase.model_validate(
        _case(
            tools=["echo", "list_files"],
            script=[
                {"tool": "echo", "arguments": {"text": "a"}},
                {"tool": "list_files", "arguments": {"path": "."}},
                {"final": "done"},
            ],
            expected={
                "status": "succeeded",
                "tools": ["echo", "list_files"],
                "tool_sequence": ["list_files", "echo"],
            },
        )
    )
    observed = _observed(
        tool_sequence=("echo", "list_files"),
        tool_arguments=({"text": "a"}, {"path": "."}),
        model_decisions=2,
        logical_tool_calls=2,
        tool_attempts=2,
    )
    verdict = judge_case(case, observed)
    assert not verdict.passed
    assert verdict.metrics.tool_set_accuracy == 1.0
    assert verdict.metrics.tool_sequence_match == 0.0
    assert any(item.startswith("sequence:") for item in verdict.failed_assertions)


def test_judge_detects_wrong_arguments_on_correct_tool() -> None:
    case = AgentCase.model_validate(
        _case(
            tools=["read_file"],
            script=[{"tool": "read_file", "arguments": {"path": "README.md"}}, {"final": "done"}],
            expected={
                "status": "succeeded",
                "tool_sequence": ["read_file"],
                "args_contains": [{"path": "app.py"}],
            },
        )
    )
    observed = _observed(tool_sequence=("read_file",), tool_arguments=({"path": "README.md"},))
    verdict = judge_case(case, observed)
    assert not verdict.passed
    assert verdict.metrics.argument_accuracy == 0.0
    assert any(item.startswith("arguments:") for item in verdict.failed_assertions)


def test_judge_detects_unexpected_tool_on_no_tool_case() -> None:
    case = AgentCase.model_validate(_case(expected={"status": "succeeded", "no_tool": True}))
    verdict = judge_case(case, _observed())
    assert not verdict.passed
    assert verdict.metrics.tool_set_accuracy == 0.0


def test_judge_detects_exhaustion_claimed_as_success() -> None:
    case = AgentCase.model_validate(_case(expected={"status": "succeeded"}))
    verdict = judge_case(case, _observed(status=RunStatus.EXHAUSTED))
    assert not verdict.passed
    assert any(item.startswith("status:") for item in verdict.failed_assertions)


def test_judge_detects_over_max_calls_and_steps() -> None:
    case = AgentCase.model_validate(
        _case(expected={"status": "succeeded", "max_tool_calls": 0, "max_steps": 1})
    )
    verdict = judge_case(case, _observed())
    assert not verdict.passed
    assert any(item.startswith("tool_calls:") for item in verdict.failed_assertions)
    assert any(item.startswith("steps:") for item in verdict.failed_assertions)


def test_max_tool_calls_uses_admitted_calls_not_model_decisions() -> None:
    """A decision refused at a budget gate is not an admitted tool call."""
    case = AgentCase.model_validate(_case(expected={"status": "succeeded", "max_tool_calls": 1}))
    admitted = _observed(model_decisions=2, logical_tool_calls=1, tool_attempts=1)
    assert judge_case(case, admitted).passed
    over = _observed(model_decisions=2, logical_tool_calls=2, tool_attempts=2)
    assert not judge_case(case, over).passed


def test_judge_aligns_arguments_by_call_position() -> None:
    """The same tool called twice is scored per position, not by name."""
    case = AgentCase.model_validate(
        _case(
            script=[
                {"tool": "read_file", "arguments": {"path": "app.py"}},
                {"tool": "read_file", "arguments": {"path": "README.md"}},
                {"final": "done"},
            ],
            tools=["read_file"],
            expected={
                "status": "succeeded",
                "tool_sequence": ["read_file", "read_file"],
                "args_contains": [{"path": "app.py"}, {"path": "README.md"}],
            },
        )
    )
    observed = _observed(
        tool_sequence=("read_file", "read_file"),
        tool_arguments=({"path": "app.py"}, {"path": "README.md"}),
        model_decisions=2,
        logical_tool_calls=2,
        tool_attempts=2,
    )
    assert judge_case(case, observed).passed


def test_planner_step_offset_is_explicit() -> None:
    """Plan-Execute's planner step is granted, then asserted, not silently lost."""
    case = AgentCase.model_validate(_case(expected={"status": "succeeded", "max_steps": 2}))
    within = _observed(steps=2 + PLANNER_STEPS)
    assert judge_case(case, within, planner_steps=PLANNER_STEPS).passed
    beyond = _observed(steps=3 + PLANNER_STEPS)
    assert not judge_case(case, beyond, planner_steps=PLANNER_STEPS).passed
    # Without the offset the same run would be rejected: the accounting matters.
    assert not judge_case(case, within).passed


def test_contract_checks_accept_execution_prefix() -> None:
    """A budget-truncated run still satisfies the wiring contract."""
    case = AgentCase.model_validate(
        _case(
            script=[
                {"tool": "echo", "arguments": {"text": "a"}},
                {"tool": "echo", "arguments": {"text": "b"}},
                {"final": "done"},
            ],
            expected={"status": "exhausted", "tools": ["echo"]},
        )
    )
    truncated = _observed(
        status=RunStatus.EXHAUSTED,
        tool_sequence=("echo",),
        tool_arguments=({"text": "a"},),
        model_decisions=1,
        logical_tool_calls=1,
        tool_attempts=1,
    )
    assert all(check.passed for check in contract_checks(case, truncated, Strategy.REACT))


def test_contract_checks_reject_unexecuted_admitted_call() -> None:
    case = AgentCase.model_validate(_case())
    mismatch = _observed(logical_tool_calls=2, tool_attempts=1)
    checks = contract_checks(case, mismatch, Strategy.REACT)
    failed = [check.name for check in checks if not check.passed]
    assert any(name.endswith("admitted_calls_were_executed") for name in failed)


def test_contract_checks_enforce_planner_lifecycle_per_strategy() -> None:
    """The check counts planner *invocations*, which is a wiring fact."""
    case = AgentCase.model_validate(_case())
    planned = _observed(planner_requests=1, plan_events=1, steps=3)
    assert all(check.passed for check in contract_checks(case, planned, Strategy.PLAN_EXECUTE))
    assert not all(check.passed for check in contract_checks(case, planned, Strategy.REACT))
    unplanned = _observed(planner_requests=0)
    assert not all(
        check.passed for check in contract_checks(case, unplanned, Strategy.PLAN_EXECUTE)
    )


def test_contract_checks_pass_when_the_planner_answered_badly() -> None:
    """A planner invocation with no valid plan is a quality failure, not a wiring one.

    The harness did drive the planner exactly once; it simply answered with
    prose. Reporting that as a broken contract would blame the harness for a
    model defect and would mask the real quality signal.
    """
    case = AgentCase.model_validate(_case())
    invoked_but_invalid = _observed(
        planner_requests=1, plan_events=0, steps=1, status=RunStatus.FAILED
    )
    assert all(
        check.passed for check in contract_checks(case, invoked_but_invalid, Strategy.PLAN_EXECUTE)
    )


def test_contract_checks_require_planner_step_to_be_counted() -> None:
    case = AgentCase.model_validate(_case())
    uncounted = _observed(planner_requests=1, plan_events=1, steps=0)
    checks = contract_checks(case, uncounted, Strategy.PLAN_EXECUTE)
    failed = [check.name for check in checks if not check.passed]
    assert any(name.endswith("planner_step_counted") for name in failed)


async def test_dev_run_covers_both_runtimes_and_separates_probes(tmp_path: Path) -> None:
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "agent-eval-dev.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )
    assert isinstance(report, AgentEvaluationReport)
    assert report.measurement_valid
    assert report.keyless_gate_passed is True
    assert report.split == "dev"
    assert {arm.strategy for arm in report.arms} == {Strategy.REACT, Strategy.PLAN_EXECUTE}
    for arm in report.arms:
        assert arm.contract_checks_passed, arm.contract_failures
        assert arm.benchmark_pass == arm.benchmark_cases
        assert arm.scorer_probes > 0
        assert arm.scorer_probes_detected == arm.scorer_probes
        assert arm.probe_detection_rate == 1.0
    assert report.scorer_probes > 0
    assert report.benchmark_cases + report.scorer_probes == len(report.cases) // len(report.arms)


async def test_probes_are_excluded_from_benchmark_metrics(tmp_path: Path) -> None:
    """The headline numbers must not be depressed by negative controls."""
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "agent-eval-dev.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )
    arm = next(item for item in report.arms if item.strategy is Strategy.REACT)
    assert arm.benchmark_pass == arm.benchmark_cases
    probes = [case for case in report.cases if case.is_probe and case.strategy is Strategy.REACT]
    assert len(probes) == arm.scorer_probes
    assert all(not case.judged_pass for case in probes)
    benchmark = [
        case for case in report.cases if not case.is_probe and case.strategy is Strategy.REACT
    ]
    assert all(case.judged_pass for case in benchmark)
    assert len(benchmark) == arm.benchmark_cases


async def test_plan_execute_counts_the_planner_step(tmp_path: Path) -> None:
    """The planner tax must be visible in the report, not hidden by the evaluator."""
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "agent-eval-dev.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )
    react = next(arm for arm in report.arms if arm.strategy is Strategy.REACT)
    planned = next(arm for arm in report.arms if arm.strategy is Strategy.PLAN_EXECUTE)
    assert planned.mean_steps - react.mean_steps == pytest.approx(PLANNER_STEPS)
    by_id: dict[str, dict[str, int]] = {}
    for case in report.cases:
        if not case.is_probe:
            by_id.setdefault(case.id, {})[case.strategy.value] = case.steps
    deltas = {
        case_id: steps["plan_execute"] - steps["react"]
        for case_id, steps in by_id.items()
        if "react" in steps and "plan_execute" in steps
    }
    assert deltas
    # Every case pays exactly one extra planning decision, regardless of budget.
    assert set(deltas.values()) == {PLANNER_STEPS}


async def test_holdout_split_runs_separately_and_without_probes(tmp_path: Path) -> None:
    """Holdout is run deliberately, contains no probes, and is reported separately."""
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "agent-eval-holdout.json",
        project_root=PROJECT_ROOT,
        split="holdout",
    )
    assert report.split == "holdout"
    assert report.scorer_probes == 0
    assert report.benchmark_cases == len(report.cases) // len(report.arms)
    assert all(not case.is_probe for case in report.cases)
    for arm in report.arms:
        assert arm.contract_checks_passed, arm.contract_failures
        assert arm.benchmark_pass == arm.benchmark_cases
    # Scorer validity is unmeasurable without probes; a vacuous 1.0 would read as
    # perfect agreement when nothing was checked.
    assert report.grading_agreement is None


async def test_probe_free_split_omits_grading_agreement(tmp_path: Path) -> None:
    output = tmp_path / "holdout.json"
    await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=output,
        project_root=PROJECT_ROOT,
        split="holdout",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["grading_agreement"] is None
    assert payload["scorer_probes"] == 0


async def test_live_profile_failed_case_is_a_quality_result_not_a_broken_gate(
    tmp_path: Path,
) -> None:
    """A live run's failing case must not be reported as an invalid evaluator.

    Uses a deterministic fake model that always answers immediately, so the
    failed assertions on multi-tool cases are a *quality* result: the wiring is
    fine, the "model" simply chose not to call tools.
    """
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "equal.json",
        project_root=PROJECT_ROOT,
        split="holdout",
        profile=Profile.EQUAL,
        model_factory=_FinalOnlyModel,
        model_name="fake-final-only",
        samples=2,
    )
    # The structural conditions still hold on a live run, so the report is usable.
    # `measurement_valid` says the data is usable, not that the model did well.
    assert report.measurement_valid
    # The keyless gate concept does not apply to a live profile.
    assert report.keyless_gate_passed is None
    assert report.profile is Profile.EQUAL
    assert report.token_source == "model_reported"
    assert report.cost_metrics_valid is True
    assert report.samples_per_case == 2
    for arm in report.arms:
        assert arm.diagnostics.synthetic_token_usage is False


class _ViolatingModel:
    """A model that always answers with two tool calls, violating the protocol.

    Stands in for the live behaviour captured by the `retr-two-searches` Bad Case,
    so capture can be tested without a real endpoint.
    """

    def __init__(self) -> None:
        self._inner = _FinalOnlyScripted()

    async def decide(self, request: ModelRequest) -> ModelResult:
        if not request.tools:
            return await self._inner.decide(request)
        calls = tuple(
            ToolCall(id=f"c{index}", name="search_code", arguments={"query": q})
            for index, q in enumerate(("a", "b"), start=1)
        )
        raise ModelProtocolError(
            "parallel tool calls are not enabled for this runtime",
            code="multiple_tool_calls_not_allowed",
            received_tool_calls=len(calls),
            usage=ModelUsage(input_tokens=20, output_tokens=8),
        )


def _violating_model() -> Model:
    return _ViolatingModel()


def _FinalOnlyModel() -> Model:
    """A deterministic non-tool-calling model, standing in for a live endpoint.

    A planner request carries no tools, so the fake answers it with a valid plan
    JSON and answers every execution request with a final answer. This exercises
    the real Plan-Execute path (a `plan.created` event and a counted planner
    step) without a tool call.
    """
    return _FinalOnlyScripted()


class _FinalOnlyScripted:
    """Minimal `Model` implementation: valid plan when planning, else a final."""

    def __init__(self) -> None:
        self._usage = ModelUsage(input_tokens=12, output_tokens=4)

    async def decide(self, request: ModelRequest) -> ModelResult:
        if not request.tools:
            plan = json.dumps({"steps": ["answer directly without calling a tool"]})
            return ModelResult(
                action=FinalAction(content=plan),
                usage=self._usage,
                model_name="fake-live-planner",
            )
        return ModelResult(
            action=FinalAction(content="no tool needed"),
            usage=self._usage,
            model_name="fake-live",
        )


async def test_live_profile_requires_a_factory_and_repeated_sampling(tmp_path: Path) -> None:
    """A live comparison without a model, or with one sample, is not a measurement."""
    with pytest.raises(ValueError, match="requires a live model factory"):
        await run_agent_evaluation(
            manifest_path=MANIFEST_PATH,
            output_path=tmp_path / "x.json",
            project_root=PROJECT_ROOT,
            split="dev",
            profile=Profile.EQUAL,
            samples=2,
        )
    with pytest.raises(ValueError, match="requires repeated sampling"):
        await run_agent_evaluation(
            manifest_path=MANIFEST_PATH,
            output_path=tmp_path / "x.json",
            project_root=PROJECT_ROOT,
            split="dev",
            profile=Profile.NATURAL,
            model_factory=_FinalOnlyModel,
            samples=1,
        )
    with pytest.raises(ValueError, match="must not be given a live model factory"):
        await run_agent_evaluation(
            manifest_path=MANIFEST_PATH,
            output_path=tmp_path / "x.json",
            project_root=PROJECT_ROOT,
            split="dev",
            profile=Profile.KEYLESS,
            model_factory=_FinalOnlyModel,
        )


async def test_force_failed_benchmark_case_invalidates_keyless_gate(tmp_path: Path) -> None:
    """A keyless failure means broken wiring, and the gate must reject it."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    broken = {
        "id": "broken-expectation",
        "category": "single_tool",
        "split": "dev",
        "task": "Echo a marker.",
        "tools": ["echo", "list_files"],
        "script": [{"tool": "echo", "arguments": {"text": "a"}}, {"final": "done"}],
        # The script never calls list_files, so this expectation cannot be met and
        # the keyless gate must fail rather than silently pass.
        "expected": {"status": "succeeded", "tools": ["list_files"]},
    }
    manifest["cases"].append(broken)
    manifest_path = tmp_path / "agent_cases.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = await run_agent_evaluation(
        manifest_path=manifest_path,
        output_path=tmp_path / "broken.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )
    # Wiring is still valid, but the keyless gate must fail: a scripted model
    # cannot legitimately miss an expectation it was scripted to satisfy.
    assert report.measurement_valid
    assert report.keyless_gate_passed is False


async def test_keyless_report_declares_cost_metrics_invalid(tmp_path: Path) -> None:
    output = tmp_path / "agent-eval-dev.json"
    await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=output,
        project_root=PROJECT_ROOT,
        split="dev",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["profile"] == "keyless"
    assert payload["token_source"] == "synthetic"
    assert payload["cost_metrics_valid"] is False
    assert payload["manifest_split_counts"]["holdout"] >= payload["thresholds"]["min_holdout_cases"]
    for arm in payload["arms"]:
        assert arm["diagnostics"]["synthetic_token_usage"] is True


async def test_report_never_exposes_a_combined_pass_ratio(tmp_path: Path) -> None:
    """Probes and benchmark cases are reported separately, never as one ratio."""
    output = tmp_path / "agent-eval-dev.json"
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=output,
        project_root=PROJECT_ROOT,
        split="dev",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert "judged_pass" not in payload
    assert "total" not in payload
    for arm in report.arms:
        assert arm.benchmark_cases > 0
        assert arm.scorer_probes > 0


async def test_repeated_runs_are_deterministic(tmp_path: Path) -> None:
    """Keyless results must be reproducible, since they gate CI."""
    first = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "a.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )
    second = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "b.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )

    def fingerprint(report: AgentEvaluationReport) -> list[tuple[str, str, bool, int, float]]:
        return sorted(
            (
                case.strategy.value,
                case.id,
                case.judged_pass,
                case.steps,
                case.tool_set_accuracy,
            )
            for case in report.cases
        )

    assert fingerprint(first) == fingerprint(second)
    assert first.manifest_sha256 == second.manifest_sha256


async def test_single_strategy_selection_is_supported(tmp_path: Path) -> None:
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "react-only.json",
        project_root=PROJECT_ROOT,
        split="dev",
        strategies=(Strategy.REACT,),
    )
    assert [arm.strategy for arm in report.arms] == [Strategy.REACT]


async def test_unknown_split_and_empty_strategies_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported split"):
        await run_agent_evaluation(
            manifest_path=MANIFEST_PATH,
            output_path=tmp_path / "x.json",
            project_root=PROJECT_ROOT,
            split="validation",
        )
    with pytest.raises(ValueError, match="at least one strategy"):
        await run_agent_evaluation(
            manifest_path=MANIFEST_PATH,
            output_path=tmp_path / "x.json",
            project_root=PROJECT_ROOT,
            split="dev",
            strategies=(),
        )


def test_evaluation_is_awaitable_from_sync_context() -> None:
    """Guard against accidental blocking in the runner entry point."""
    coroutine = run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=Path("/tmp/forgeharness-agent-eval-guard.json"),
        project_root=PROJECT_ROOT,
        split="dev",
    )
    assert asyncio.iscoroutine(coroutine)
    coroutine.close()


def test_equal_resource_budget_is_identical_for_both_arms() -> None:
    """Equal resources means one shared whole-run ceiling and no planner grant.

    This is the load-bearing methodological claim of the equal profile: any
    capacity Plan-Execute loses to planning is a real property of the strategy,
    not something the harness compensated for.
    """
    case = AgentCase.model_validate(_case())
    react = whole_run_budget(case, Strategy.REACT)
    planned = whole_run_budget(case, Strategy.PLAN_EXECUTE)
    assert react == planned
    assert react.max_steps == EQUAL_TOTAL_RUN_STEPS
    # Crucially, no extra step is granted to the planner in this profile.
    assert planned.max_steps == EQUAL_TOTAL_RUN_STEPS


def test_natural_cost_budget_grants_the_planner_step() -> None:
    """The natural profile gives both arms room, so cost reflects real overhead."""
    case = AgentCase.model_validate(_case())
    react = natural_cost_budget(case, Strategy.REACT)
    planned = natural_cost_budget(case, Strategy.PLAN_EXECUTE)
    assert planned.max_steps == react.max_steps + PLANNER_STEPS
    # Generous enough that truncation is not the explanation for a failure.
    assert react.max_steps >= 20
    assert react.max_tool_calls >= 20


def test_keyless_budget_grant_is_an_adapter_contract_not_resource_equality() -> None:
    """The keyless grant must stay separate from the equal-resource definition."""
    case = AgentCase.model_validate(_case())
    react = executor_step_budget(case, Strategy.REACT)
    planned = executor_step_budget(case, Strategy.PLAN_EXECUTE)
    assert planned.max_steps == react.max_steps + PLANNER_STEPS
    # The two profiles must not coincide, or the distinction is cosmetic.
    assert planned.max_steps != whole_run_budget(case, Strategy.PLAN_EXECUTE).max_steps


def test_case_budget_override_applies_only_keylessly() -> None:
    """A keyless override is calibrated to one ceiling; live profiles ignore it."""
    case = AgentCase.model_validate(_case(budget={"max_steps": 2}))
    assert executor_step_budget(case, Strategy.REACT).max_steps == 2
    # A live profile uses the profile budget, not the keyless override.
    assert live_budget(case, Strategy.REACT, Profile.EQUAL).max_steps == EQUAL_TOTAL_RUN_STEPS
    assert live_budget(case, Strategy.REACT, Profile.NATURAL).max_steps == NATURAL_MAX_STEPS
    with pytest.raises(ValueError, match="live profile"):
        live_budget(case, Strategy.REACT, Profile.KEYLESS)


async def test_sampling_reports_pass_rate_and_requires_every_sample(
    tmp_path: Path,
) -> None:
    """A case solved in some samples but not all is not a reliable solve.

    The fake model never calls a tool, so every case that requires one fails in
    every sample and no case is credited as a reliable solve.
    """
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "sampled.json",
        project_root=PROJECT_ROOT,
        split="dev",
        profile=Profile.EQUAL,
        model_factory=_FinalOnlyModel,
        samples=3,
    )
    assert report.samples_per_case == 3
    # Cases that require a tool can never pass under a model that never calls one.
    tool_cases = [
        case for case in report.cases if case.expected_grade == "pass" and not case.no_tool_required
    ]
    assert tool_cases
    for case in tool_cases:
        assert case.samples == 3
        assert case.passes == 0
        assert case.pass_rate == 0.0
        assert not case.judged_pass
        # Every sample's failure reason is retained, not just the last one's.
        assert case.failed_assertions


async def test_report_records_the_budget_each_arm_received(tmp_path: Path) -> None:
    """Two profiles must be distinguishable from their reports alone."""
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "budgets.json",
        project_root=PROJECT_ROOT,
        split="dev",
        profile=Profile.EQUAL,
        model_factory=_FinalOnlyModel,
        samples=2,
    )
    budgets = report.budgets
    assert set(budgets) == {"react", "plan_execute"}
    react_steps = {item["max_steps"] for item in budgets["react"].values()}
    planned_steps = {item["max_steps"] for item in budgets["plan_execute"].values()}
    # Equal profile: the same ceiling for both arms, no planner grant anywhere.
    assert react_steps == planned_steps == {EQUAL_TOTAL_RUN_STEPS}


async def test_wall_time_is_stated_not_budgeted(tmp_path: Path) -> None:
    """Wall time is measured, so the report must not imply a wall-clock ceiling."""
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "wall.json",
        project_root=PROJECT_ROOT,
        split="dev",
        profile=Profile.EQUAL,
        model_factory=_FinalOnlyModel,
        samples=2,
    )
    assert report.wall_time_ms >= 0
    assert report.model_name == "scripted"  # default when no live name is given


def test_live_config_bounds_generation_and_disables_thinking() -> None:
    """An unbounded per-request ceiling is what exhausts a local server's memory."""
    config = live_eval_config(
        base_url="http://127.0.0.1:8000/v1", api_key="local", model="some-model"
    )
    assert config.max_tokens == LIVE_MAX_OUTPUT_TOKENS
    assert config.enable_thinking is False
    assert config.temperature == 0.0
    # A caller may lower the ceiling but must never leave it unset.
    assert (
        live_eval_config(
            base_url="http://127.0.0.1:8000/v1",
            api_key="local",
            model="m",
            max_tokens=256,
        ).max_tokens
        == 256
    )


async def test_truncated_run_is_incomplete_and_not_a_measurement(tmp_path: Path) -> None:
    """A partial split must never be citable as a valid measurement."""
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "partial.json",
        project_root=PROJECT_ROOT,
        split="dev",
        limit=3,
    )
    assert report.complete_split is False
    assert report.measurement_valid is False
    assert report.keyless_gate_passed is False
    # Only the requested number of cases ran.
    assert report.benchmark_cases + report.scorer_probes == 3


async def test_limit_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await run_agent_evaluation(
            manifest_path=MANIFEST_PATH,
            output_path=tmp_path / "x.json",
            project_root=PROJECT_ROOT,
            split="dev",
            limit=0,
        )


async def test_untruncated_run_is_complete(tmp_path: Path) -> None:
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "full.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )
    assert report.complete_split is True
    assert report.measurement_valid is True


async def test_live_profiles_exclude_keyless_only_cases(tmp_path: Path) -> None:
    """Budget-mechanism cases and probes must not dilute a live comparison.

    `budget_boundary` cases assert a specific `EXHAUSTED` outcome under a
    per-case budget override, but a live profile deliberately applies one shared
    budget. Those cases would therefore fail by construction and add always-failing
    noise to the comparison.
    """
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "live.json",
        project_root=PROJECT_ROOT,
        split="dev",
        profile=Profile.EQUAL,
        model_factory=_FinalOnlyModel,
        samples=2,
    )
    assert report.scorer_probes == 0
    assert all(not case.is_probe for case in report.cases)
    assert all(case.category is not CaseCategory.BUDGET_BOUNDARY for case in report.cases)
    # Excluding those cases must not invalidate the measurement.
    assert report.measurement_valid is True
    assert report.grading_agreement is None


def test_keyless_reports_still_include_every_category() -> None:
    """The live-only filter must not weaken the keyless gate's coverage."""
    manifest = _manifest()
    dev_categories = {case.category for case in manifest.for_split("dev")}
    assert CaseCategory.BUDGET_BOUNDARY in dev_categories
    assert KEYLESS_ONLY_CATEGORIES == {CaseCategory.BUDGET_BOUNDARY}


async def test_bad_case_artifacts_stay_under_the_output_path(tmp_path: Path) -> None:
    """Evaluation artifacts must be written under the report's own output path.

    A fixed repo path makes every test run and exploratory evaluation pollute the
    working tree. This compares the repository before and after rather than
    asserting a fixed path is absent, because a real capture repository legitimately
    contains checked-in evidence at that location.
    """
    repo_artifacts = PROJECT_ROOT / "reports" / "bad-cases"
    before = sorted(p.name for p in repo_artifacts.glob("*")) if repo_artifacts.exists() else []
    output = tmp_path / "agent-eval.json"
    await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=output,
        project_root=PROJECT_ROOT,
        split="dev",
        profile=Profile.EQUAL,
        model_factory=_violating_model,
        samples=2,
    )
    bad_case_dir = tmp_path / "bad-cases"
    assert bad_case_dir.is_dir()
    assert list(bad_case_dir.glob("*.json"))
    # The repository's own artifacts are untouched by this run.
    after = sorted(p.name for p in repo_artifacts.glob("*")) if repo_artifacts.exists() else []
    assert after == before


async def test_bad_case_binds_trace_evidence(tmp_path: Path) -> None:
    """A Bad Case must point back at verifiable trace evidence."""
    output = tmp_path / "agent-eval.json"
    await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=output,
        project_root=PROJECT_ROOT,
        split="dev",
        profile=Profile.EQUAL,
        model_factory=_violating_model,
        samples=2,
    )
    records = sorted((tmp_path / "bad-cases").glob("*.json"))
    record = BadCase.model_validate_json(records[0].read_text(encoding="utf-8"))
    assert record.revision
    assert record.profile == "equal"
    assert record.model == "scripted"
    # The referenced trace exists, its bytes match the recorded hash, and the
    # authoritative hash chain still verifies independently.
    trace = tmp_path / "bad-cases" / record.trace_path
    assert trace.is_file()
    assert hashlib.sha256(trace.read_bytes()).hexdigest() == record.trace_hash
    assert verify_trace(trace).valid


async def test_probes_are_never_captured_as_bad_cases(tmp_path: Path) -> None:
    """A probe is designed to fail, so it must not become evidence.

    Capturing probes would flood the Bad Case directory with entries describing no
    real defect, because every probe fails by construction in every run.
    """
    output = tmp_path / "agent-eval.json"
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=output,
        project_root=PROJECT_ROOT,
        split="dev",
    )
    # The keyless run grades 7 probes, all of which fail by design.
    assert report.scorer_probes > 0
    for arm in report.arms:
        assert arm.scorer_probes_detected == arm.scorer_probes
    records = (
        list((tmp_path / "bad-cases").glob("*.json")) if (tmp_path / "bad-cases").exists() else []
    )
    assert records == []


def test_constraint_effectiveness_requires_a_real_binding_share() -> None:
    """A ceiling that never binds must not be reported as a constraint.

    The equal-resource profile exists to answer "who wins under the same resource
    ceiling". If the ceiling never binds, that profile is indistinguishable from
    the natural-cost one and cannot support a resource-limited conclusion.
    """

    def outcome(case_id: str, steps: int) -> CaseOutcome:
        return CaseOutcome(
            id=case_id,
            strategy=Strategy.REACT,
            category=CaseCategory.SINGLE_TOOL,
            split="dev",
            is_probe=False,
            expected_grade="pass",
            judged_pass=True,
            agreed=True,
            failed_assertions=(),
            tool_set_accuracy=1.0,
            forbidden_violation=False,
            status=RunStatus.SUCCEEDED,
            steps=steps,
            logical_tool_calls=1,
            tool_attempts=1,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1.0,
        )

    budgets = {"react": {"a": {"max_steps": 4}, "b": {"max_steps": 4}}}

    # Nothing reaches the ceiling -> the constraint is not effective.
    effective, share = constraint_binding(
        budgets=budgets, outcomes=(outcome("a", 2), outcome("b", 1))
    )
    assert effective is False
    assert share == 0.0

    # Exactly one of ten reaches it -> 10%, which meets the rule.
    outcomes = tuple(outcome(f"c{i}", 4 if i == 0 else 1) for i in range(10))
    budgets_ten = {"react": {f"c{i}": {"max_steps": 4} for i in range(10)}}
    effective, share = constraint_binding(budgets=budgets_ten, outcomes=outcomes)
    assert effective is True
    assert share == pytest.approx(0.10)

    # Just under the threshold is not enough.
    outcomes = tuple(outcome(f"d{i}", 4 if i == 0 else 1) for i in range(20))
    budgets_twenty = {"react": {f"d{i}": {"max_steps": 4} for i in range(20)}}
    effective, _ = constraint_binding(budgets=budgets_twenty, outcomes=outcomes)
    assert effective is False

    assert constraint_binding(budgets={}, outcomes=()) == (None, None)


async def test_report_pins_experiment_identity(tmp_path: Path) -> None:
    """A report must carry the versions that make a comparison cross-version."""
    report = await run_agent_evaluation(
        manifest_path=MANIFEST_PATH,
        output_path=tmp_path / "identity.json",
        project_root=PROJECT_ROOT,
        split="dev",
    )
    assert report.observation_contract_version == OBSERVATION_CONTRACT_VERSION
    assert report.recovery_semantics_version == RECOVERY_SEMANTICS_VERSION
    assert report.benchmark_version == BENCHMARK_VERSION
    assert report.budget_profile_version == BUDGET_PROFILE_VERSION
    # The keyless profile claims no binding constraint, so the field is absent
    # rather than a misleading False.
    assert report.constraint_effective is None
    assert report.constrained_run_share is None

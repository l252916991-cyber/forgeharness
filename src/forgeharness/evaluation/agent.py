"""Keyless Agent Benchmark: frozen orchestration metrics over a fixed case set.

This module owns the metric definitions for the next-phase Agent Benchmark
(see `docs/NEXT_PHASE_PLAN.md`). Three separations are deliberate:

* **Benchmark cases vs scorer probes.** Cases carry ``grade: "pass" | "fail"``.
  Probes are deliberately-broken samples used only to prove the scorer detects
  real deviations. They are excluded from every headline metric, so a report
  never reads as "the agent passed 54 of 61" when seven of those are negative
  controls.
* **Assertions vs continuous metrics.** ``judge_case`` applies the hard
  assertions in a case's ``expected`` block. Continuous metrics are reported for
  trend analysis and never decide a verdict.
* **Keyless vs live.** A scripted model replaces the decision under test, so the
  keyless profile validates the grading contract, the metric pipeline, and the
  wiring of **both** runtimes. It cannot show that one orchestration strategy is
  smarter than the other; that requires live-model runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel, Field, model_validator

from forgeharness.coding.tools import (
    ListFilesTool,
    ReadFileTool,
    SearchCodeTool,
    WriteFileTool,
)
from forgeharness.domain.models import (
    CURRENT_RECOVERY_SEMANTICS_VERSION,
    FinalAction,
    FrozenModel,
    ModelResult,
    ModelUsage,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    TraceEvent,
)
from forgeharness.evaluation.bad_case import (
    BadCase,
    FailureClass,
    FailureEvidence,
    ReplayEligibility,
    ReplayMode,
    ReplayStep,
    classify_failure,
    executor_replay_script,
    fixture_gap_note,
    no_progress_diagnosis,
    persist_trace,
    planner_replay_script,
    projected_step_summary,
    write_bad_case,
)
from forgeharness.models.base import Model
from forgeharness.models.openai_compatible import OpenAICompatibleConfig
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.steps import StepActionKind, project_events
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.plan_execute import PlanExecuteRuntime
from forgeharness.tools.base import Tool, ToolContext, ToolOutput, ToolSpec
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.process import run_command
from forgeharness.tools.registry import ToolRegistry

BENCHMARK_TOOL_NAMES = ("echo", "list_files", "read_file", "search_code", "write_file")

# A fresh model per arm keeps the two arms independent; one model per planner and
# executor keeps a Plan-Execute run's planning and execution on the same endpoint.
ModelFactory = Callable[[], "Model"]

# A split is either the visible development set or the frozen holdout set. The
# holdout set is never run by CI; see the module docstring and the project plan.
Split = Literal["dev", "holdout"]

# Plan-Execute spends exactly one decision on planning before the executor loop
# starts. Assertions and reporting must account for it explicitly rather than
# pretending both runtimes consume the same number of steps.
PLANNER_STEPS = 1


# Experiment-identity versions. These exist because runtime behaviour is itself an
# experimental variable: a change to how failures are surfaced, retried or
# recovered alters what the model sees and therefore what it does. Bumping a
# version makes a cross-version comparison visibly cross-version instead of
# letting two different runtimes be plotted as one "improving agent" line.
#
# - OBSERVATION_CONTRACT_VERSION: the shape of what the model receives on failure.
#   Bumped when tool failures became structured JSON instead of raw exception text,
#   because that alone changed a case from 4 steps to 11.
# - RECOVERY_SEMANTICS_VERSION: retry/recovery behaviour. See the generation list in
#   `domain/models.py`; the current freeze is 2 (journal + retry/attempts + soft
#   deadline). A run using the journal has different side-effect semantics from one
#   that does not, so the two must never be compared as if only the agent had
#   changed.
OBSERVATION_CONTRACT_VERSION = 1
RECOVERY_SEMANTICS_VERSION = CURRENT_RECOVERY_SEMANTICS_VERSION
BENCHMARK_VERSION = 1
# The budget profile definitions (equal vs natural ceilings) are versioned so a
# later recalibration is recorded rather than silently replacing the old rules.
# 1 = the hand-written equal step cap of 12, which never bound (0% of runs) and made
#     the equal profile indistinguishable from natural.
# 2 = the cap derived by the frozen M8-C rule from the post-M10-C dev-natural pooled
#     P75: cap=4, binding 29.4% of runs.
BUDGET_PROFILE_VERSION = 2


class Strategy(StrEnum):
    """Orchestration runtimes the unified evaluator can drive."""

    REACT = "react"
    PLAN_EXECUTE = "plan_execute"


class Profile(StrEnum):
    """Which execution regime a report measures.

    The two live profiles answer different questions and must never be merged:
    ``EQUAL`` shares one whole-run budget across arms, so it answers "who does
    better with the same resources". ``NATURAL`` gives both arms enough room to
    finish, so the planner overhead appears in the reported cost rather than
    being hidden by a truncated run.
    """

    KEYLESS = "keyless"
    EQUAL = "equal"
    NATURAL = "natural"


class CaseCategory(StrEnum):
    """Coverage classes required by the benchmark design."""

    SINGLE_TOOL = "single_tool"
    MULTI_TOOL_SERIAL = "multi_tool_serial"
    RETRIEVAL_THEN_TOOL = "retrieval_then_tool"
    AMBIGUOUS_ARGS = "ambiguous_args"
    TOOL_FAILURE_RECOVERY = "tool_failure_recovery"
    LONG_HORIZON = "long_horizon"
    NO_TOOL = "no_tool"
    BUDGET_BOUNDARY = "budget_boundary"


# Categories that test the harness mechanism rather than agent quality, so they
# only make sense in the keyless profile. `budget_boundary` cases assert specific
# `EXHAUSTED` outcomes under a per-case budget override, while a live profile
# deliberately applies one shared profile budget (ignoring the override) so the
# arms stay resource-matched. Under a live profile those expectations are
# unsatisfiable by construction, which would add always-failing cases that dilute
# a comparison without measuring anything.
KEYLESS_ONLY_CATEGORIES = frozenset({CaseCategory.BUDGET_BOUNDARY})


class InjectionMode(StrEnum):
    """Deterministic transient failures a wrapper tool can raise."""

    ERROR = "error"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"


class ScriptStep(FrozenModel):
    """One decision the keyless model emits: a tool call or the final answer."""

    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    final: str | None = None

    @model_validator(mode="after")
    def exactly_one_decision(self) -> Self:
        if (self.tool is None) == (self.final is None):
            raise ValueError("a script step sets exactly one of `tool` or `final`")
        return self


class FailureInjection(FrozenModel):
    """Inject a transient failure into one tool at a chosen invocation."""

    tool: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    mode: InjectionMode = InjectionMode.ERROR


class ExpectedOutcome(FrozenModel):
    """The hard assertions one case must satisfy to be judged as passed.

    ``tools`` is a *required* set (every name must appear) rather than an exact
    set, so recovery and long-horizon cases can legitimately repeat calls.
    Upper bounds and bans are expressed separately.
    """

    status: RunStatus
    tools: tuple[str, ...] = ()
    tool_sequence: tuple[str, ...] | None = None
    max_tool_calls: int | None = Field(default=None, ge=0)
    forbidden_tools: tuple[str, ...] = ()
    args_contains: tuple[dict[str, Any] | None, ...] | None = None
    no_tool: bool = False
    max_steps: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def aligned_arguments(self) -> Self:
        if self.args_contains is None:
            return self
        if self.tool_sequence is None:
            raise ValueError("args_contains requires tool_sequence for positional alignment")
        if len(self.args_contains) != len(self.tool_sequence):
            raise ValueError("args_contains must be as long as tool_sequence")
        return self


class AgentCase(FrozenModel):
    """One immutable benchmark case: fixture, script, grading contract, budget."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    category: CaseCategory
    split: Literal["dev", "holdout"]
    task: str = Field(min_length=1)
    tools: tuple[str, ...] = Field(min_length=1)
    script: tuple[ScriptStep, ...] = Field(min_length=1)
    expected: ExpectedOutcome
    # Probes are negative controls: they must be graded as failures, and they are
    # excluded from every headline benchmark metric.
    grade: Literal["pass", "fail"] = "pass"
    workspace: dict[str, str] = Field(default_factory=dict)
    inject_failure: FailureInjection | None = None
    budget: RunBudget | None = None
    notes: str | None = None

    @property
    def is_probe(self) -> bool:
        """True when the case exists only to prove the scorer detects deviation."""
        return self.grade == "fail"

    @model_validator(mode="after")
    def required_tools_are_available(self) -> Self:
        available = set(self.tools)
        missing = set(self.expected.tools) - available
        if missing:
            raise ValueError(f"expected tools are not available: {sorted(missing)}")
        unknown = available - set(BENCHMARK_TOOL_NAMES)
        if unknown:
            raise ValueError(f"unknown benchmark tools: {sorted(unknown)}")
        if self.expected.no_tool and self.expected.tools:
            raise ValueError("no_tool cases must not require tools")
        return self


class AgentCaseManifest(FrozenModel):
    """Versioned case list plus the distribution invariants the suite maintains."""

    schema_version: int = Field(ge=1)
    min_holdout: int = Field(default=15, ge=1)
    cases: tuple[AgentCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def enforceable_distribution(self) -> Self:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("agent case ids must be unique")
        holdout = [case for case in self.cases if case.split == "holdout"]
        if len(holdout) < self.min_holdout:
            raise ValueError(
                f"holdout split needs at least {self.min_holdout} cases, found {len(holdout)}"
            )
        present = {case.category for case in self.cases}
        absent = set(CaseCategory) - present
        if absent:
            raise ValueError(
                f"categories without coverage: {sorted(item.value for item in absent)}"
            )
        return self

    def for_split(self, split: str) -> tuple[AgentCase, ...]:
        """Return the cases belonging to one split."""
        return tuple(case for case in self.cases if case.split == split)


class ObservedRun(FrozenModel):
    """Harness-observed facts about one execution, with no model self-report.

    Two call counts are tracked because they diverge at a budget boundary:

    * ``model_decisions`` — tool calls the model emitted, read from the trace.
      This includes a final decision that a budget gate then refuses.
    * ``logical_tool_calls`` — calls admitted past the budget gates, read from
      ``usage.tool_calls``. This is the retry-independent count the frozen
      ``max_tool_calls`` definition refers to.
    * ``tool_attempts`` — executions actually started.

    ``tool_sequence`` is the decision sequence, so ordering assertions see what
    the model asked for rather than what the budget allowed.
    """

    status: RunStatus
    tool_sequence: tuple[str, ...]
    tool_arguments: tuple[dict[str, Any], ...]
    plan_events: int = Field(ge=0)
    # Planner invocations, counted even when the planner answered badly. A wiring
    # check needs this, because "planner produced a valid plan" is a quality fact.
    planner_requests: int = Field(ge=0)
    steps: int = Field(ge=0)
    model_decisions: int = Field(ge=0)
    logical_tool_calls: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    error: str | None = None


class CaseMetrics(FrozenModel):
    """Continuous metrics; reported for trend analysis, never for verdicts."""

    tool_set_accuracy: float = Field(ge=0, le=1)
    tool_sequence_match: float | None = Field(default=None, ge=0, le=1)
    argument_accuracy: float | None = Field(default=None, ge=0, le=1)
    forbidden_violation: bool


class CaseJudgement(FrozenModel):
    """Verdict for one case plus the metrics recorded alongside it."""

    passed: bool
    failed_assertions: tuple[str, ...]
    metrics: CaseMetrics


class CaseFailureEvidence(FrozenModel):
    """One failing sample kept long enough to be persisted as a Bad Case.

    Carries the raw trace events rather than a summary, because a Bad Case must
    point back at what actually happened.
    """

    sample_index: int = Field(ge=0)
    observed: ObservedRun
    judgement: CaseJudgement
    events: tuple[TraceEvent, ...]


class ContractCheck(FrozenModel):
    """Structural invariants that must hold for an arm regardless of pass rate.

    These validate the *evaluator wiring*, not agent quality: they catch a
    runtime whose counting, ordering, or planner lifecycle does not match what
    the unified evaluator assumes.
    """

    name: str
    passed: bool
    detail: str = ""


class CaseOutcome(FrozenModel):
    """One case's expected grade, observed verdict, and reproducibility metrics.

    With repeated live sampling, ``judged_pass`` means **every** sample passed and
    ``pass_rate`` is the per-attempt success fraction; the numeric fields are the
    per-sample median so one unlucky sample cannot skew the aggregate. At
    ``samples == 1`` the two agree exactly.
    """

    id: str
    strategy: Strategy
    category: CaseCategory
    split: Literal["dev", "holdout"]
    is_probe: bool
    expected_grade: Literal["pass", "fail"]
    # Whether the contract forbade tool use, so a reader can tell a no-tool case
    # apart from a case whose required tool happened to be missing.
    no_tool_required: bool = False
    judged_pass: bool
    agreed: bool
    failed_assertions: tuple[str, ...]
    samples: int = Field(default=1, ge=1)
    passes: int = Field(default=0, ge=0)
    pass_rate: float = Field(default=0.0, ge=0, le=1)
    tool_set_accuracy: float = Field(ge=0, le=1)
    tool_sequence_match: float | None = Field(default=None, ge=0, le=1)
    argument_accuracy: float | None = Field(default=None, ge=0, le=1)
    forbidden_violation: bool
    status: RunStatus
    steps: int = Field(ge=0)
    logical_tool_calls: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)


class PipelineDiagnostics(FrozenModel):
    """Non-headline diagnostics for the keyless pipeline.

    Token figures here are synthetic: a scripted model has no provider usage.
    They exist to exercise the aggregation path and are deliberately not
    promoted to cost metrics.
    """

    synthetic_token_usage: bool
    mean_input_tokens: float = Field(ge=0)
    mean_output_tokens: float = Field(ge=0)
    mean_tool_attempts: float = Field(ge=0)


class ArmSummary(FrozenModel):
    """Aggregate for one orchestration strategy over one split.

    All metrics cover benchmark cases only; scorer probes are counted separately
    so that a probe can never depress a quality number.

    ``benchmark_pass`` counts cases solved in **every** sample (reliable
    solutions); ``mean_pass_rate`` averages the per-attempt success fraction. At
    one sample the two coincide, so both are always reported rather than
    switching meaning with the sampling count.
    """

    strategy: Strategy
    benchmark_cases: int = Field(ge=0)
    benchmark_pass: int = Field(ge=0)
    mean_pass_rate: float = Field(default=0.0, ge=0, le=1)
    scorer_probes: int = Field(ge=0)
    scorer_probes_detected: int = Field(ge=0)
    probe_detection_rate: float = Field(ge=0, le=1)
    contract_checks_passed: bool
    contract_failures: tuple[str, ...]
    tool_set_accuracy: float = Field(ge=0, le=1)
    tool_sequence_match: float | None = Field(default=None, ge=0, le=1)
    argument_accuracy: float | None = Field(default=None, ge=0, le=1)
    mean_steps: float = Field(ge=0)
    mean_logical_tool_calls: float = Field(ge=0)
    diagnostics: PipelineDiagnostics


class CategorySummary(FrozenModel):
    """Aggregate over one category for one strategy, excluding probes."""

    strategy: Strategy
    category: CaseCategory
    cases: int = Field(ge=0)
    judged_pass: int = Field(ge=0)
    tool_set_accuracy: float = Field(ge=0, le=1)
    argument_accuracy: float | None = Field(default=None, ge=0, le=1)


class AgentEvaluationReport(FrozenModel):
    """Machine-readable benchmark evidence bound to a source revision.

    Two verdicts are kept apart so neither can be misread:

    * ``measurement_valid`` — is this experiment's data structurally usable?
      True when at least one benchmark case was graded, every category is
      covered, the manifest still has an adequate holdout split, and both arms
      satisfied their wiring contract. It says nothing about model quality, so a
      live run that fails many cases can still be a valid measurement.
    * ``keyless_gate_passed`` — the keyless-only gate: every scorer probe was
      detected and every benchmark case passed. ``None`` on a live profile,
      where the concept does not apply, rather than a misleading ``False``.

    ``cost_metrics_valid`` is false for the keyless profile: token figures are
    synthetic and must never enter a cost comparison. Only a live-model profile
    may report ``token_source: "model_reported"``.
    """

    schema_version: int
    created_at: datetime
    revision: str
    profile: Profile
    model_name: str
    # The provider-side model parameters must be pinned with the name: the same
    # model at a different temperature or output ceiling is a different experiment.
    # Named `model_parameters` because pydantic reserves `model_config`.
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    split: Literal["dev", "holdout"]
    # Experiment identity. See the version constants for why these are recorded.
    observation_contract_version: int = OBSERVATION_CONTRACT_VERSION
    recovery_semantics_version: int = RECOVERY_SEMANTICS_VERSION
    benchmark_version: int = BENCHMARK_VERSION
    budget_profile_version: int = BUDGET_PROFILE_VERSION
    token_source: Literal["synthetic", "model_reported"]
    cost_metrics_valid: bool
    # Whole-run budgets actually applied, so two reports can be compared without
    # guessing whether they were resource-matched.
    budgets: dict[str, dict[str, dict[str, int]]]
    samples_per_case: int = Field(ge=1)
    # Whether the equal-resource ceiling actually bound. A profile that claims to
    # constrain resources but never does is indistinguishable from the
    # natural-cost profile, so any resource-limited conclusion requires
    # `constraint_effective: true`. None for the keyless and natural profiles,
    # which do not claim a binding constraint.
    constraint_effective: bool | None = None
    constrained_run_share: float | None = Field(default=None, ge=0, le=1)
    # False when the run was deliberately truncated (--limit). A partial split is
    # exploratory: it cannot be usable as a measurement and no figure from it
    # should be cited as one.
    complete_split: bool = True
    # Wall time is **measured, not budgeted**, in these profiles. The runtime supports
    # a ceiling (`RunBudget.max_wall_seconds`) but neither profile sets one, on
    # purpose: the frozen calibration rule gives the equal profile exactly one main
    # binding constraint (total steps), and adding a second would make a failure
    # unattributable. So this field reports elapsed time; it is not a limit.
    wall_time_ms: float = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    # Hash of the sorted holdout case ids. The file hash above already pins the bytes,
    # but this pins the *evaluation set*: if the dev cases are ever edited while the
    # holdout set is untouched, a holdout report still matches the frozen set. It is
    # also what the freeze manifest records, so a report can be tied to a manifest.
    holdout_case_ids_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_split_counts: dict[str, int]
    benchmark_cases: int = Field(ge=0)
    scorer_probes: int = Field(ge=0)
    # Absent when the run split has no probes, because scorer validity is then
    # unmeasurable. Never report a vacuous 1.0.
    grading_agreement: float | None = Field(default=None, ge=0, le=1)
    measurement_valid: bool
    keyless_gate_passed: bool | None = None
    thresholds: dict[str, float]
    arms: tuple[ArmSummary, ...]
    categories: tuple[CategorySummary, ...]
    cases: tuple[CaseOutcome, ...]


class _AllowAllPolicy:
    """The benchmark isolates orchestration, so policy never suspends a case.

    Approval and denial behavior is covered by the deterministic control suite
    (`forge eval-control`); mixing it in here would measure policy, not planning.
    """

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="benchmark isolates policy")


class _InjectedFailureTool:
    """Delegate to a real tool but fail deterministically on one invocation."""

    def __init__(self, inner: Tool, *, attempt: int, mode: InjectionMode) -> None:
        self._inner = inner
        self._attempt = attempt
        self._mode = mode
        self._count = 0

    @property
    def spec(self) -> ToolSpec:
        return self._inner.spec

    @property
    def input_model(self) -> type[BaseModel]:
        return self._inner.input_model

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        self._count += 1
        if self._count == self._attempt:
            if self._mode is InjectionMode.TIMEOUT:
                raise TimeoutError("injected transport timeout")
            if self._mode is InjectionMode.NOT_FOUND:
                return ToolOutput(ok=False, content="injected: requested resource not found")
            raise RuntimeError("injected transient backend failure")
        return await self._inner.execute(arguments, context)


def _build_registry(case: AgentCase) -> ToolRegistry:
    available: dict[str, Tool] = {
        "echo": EchoTool(),
        "list_files": ListFilesTool(),
        "read_file": ReadFileTool(),
        "search_code": SearchCodeTool(),
        "write_file": WriteFileTool(),
    }
    registry = ToolRegistry()
    for name in case.tools:
        tool = available[name]
        if case.inject_failure is not None and case.inject_failure.tool == name:
            tool = _InjectedFailureTool(
                tool,
                attempt=case.inject_failure.attempt,
                mode=case.inject_failure.mode,
            )
        registry.register(tool)
    return registry


def _scripted_responses(case: AgentCase) -> list[ModelResult]:
    """Build deterministic decisions plus synthetic token usage.

    A scripted model has no provider usage, so each decision reports a small
    deterministic token count. This keeps the token aggregation path exercised;
    the report labels the source as synthetic and marks cost metrics invalid so
    the figures are never read as cost measurements.
    """
    responses: list[ModelResult] = []
    for index, step in enumerate(case.script, start=1):
        usage = ModelUsage(input_tokens=10 * index, output_tokens=5)
        if step.tool is not None:
            responses.append(
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(
                            id=f"{case.id}-call-{index}", name=step.tool, arguments=step.arguments
                        )
                    ),
                    usage=usage,
                    model_name="scripted",
                )
            )
        else:
            responses.append(
                ModelResult(
                    action=FinalAction(content=step.final or ""),
                    usage=usage,
                    model_name="scripted",
                )
            )
    return responses


def _plan_steps(case: AgentCase) -> list[str]:
    """Derive a valid plan from the scripted tool decisions."""
    tools = [step.tool for step in case.script if step.tool is not None]
    if not tools:
        return ["answer directly without calling a tool"]
    return [f"{index}. call {name}" for index, name in enumerate(tools, start=1)]


def executor_step_budget(case: AgentCase, strategy: Strategy) -> RunBudget:
    """Return the *executor* step budget for the keyless profile.

    Naming matters here, because two different budgets exist:

    * ``executor_step_budget`` (this function) — how many decisions the executor
      loop may spend. A Plan-Execute run pays one decision for planning before
      that loop starts, so the planner step is added explicitly. Without it,
      ``max_steps=1`` would let ReAct execute one tool call while denying
      Plan-Execute any execution, making the arms incomparable.
    * ``total_run_steps`` — the whole-run step ceiling, planner included. This is
      what the equal-resource profile shares across arms; see
      ``whole_run_budget``. Granting a step is an *adapter contract* used when the
      goal is to compare execution phases, never the definition of equal
      resources.

    The matching offset in ``judge_case`` keeps the step assertion on the same
    footing. Live profiles use ``live_budget`` instead, which deliberately does
    not apply this grant to the equal-resource case.
    """
    base = case.budget or RunBudget()
    if strategy is Strategy.REACT:
        return base
    return base.model_copy(update={"max_steps": base.max_steps + PLANNER_STEPS})


# Generous but bounded: enough that a well-behaved run finishes, small enough that
# a looping model still terminates and is reported as exhausted.
NATURAL_MAX_STEPS = 24
NATURAL_MAX_TOOL_CALLS = 24
NATURAL_MAX_INPUT_TOKENS = 200_000
NATURAL_MAX_OUTPUT_TOKENS = 40_000

# One shared whole-run ceiling for every arm in the equal-resource profile. The
# value is the same for both runtimes, which is the entire point: Plan-Execute's
# planner decision is spent from this ceiling, not added on top of it.
#
# The step ceiling is **derived, not chosen**: the frozen rule
# (docs/M8C_CALIBRATION_RULES.md) takes the pooled dev-natural P75 of
# `total_run_steps`, rounds up, and requires that at least 10% of runs actually bind
# (`constraint_binding`). Hand-picking a number would make the threshold
# unfalsifiable. Re-derived after the M10-B regression: pooled P75 = 4.00 -> 4,
# binding 20/68 = 29.4%.
EQUAL_TOTAL_RUN_STEPS = 4
EQUAL_MAX_TOOL_CALLS = 12
EQUAL_MAX_INPUT_TOKENS = 60_000
EQUAL_MAX_OUTPUT_TOKENS = 20_000


def natural_cost_budget(case: AgentCase, strategy: Strategy) -> RunBudget:
    """Return a budget generous enough that neither arm is truncated.

    The planner grant still applies, so a Plan-Execute run is not artificially
    starved; its extra planning decision then shows up in the measured cost,
    which is exactly what this profile is for.
    """
    del case
    base = RunBudget(
        max_steps=NATURAL_MAX_STEPS,
        max_tool_calls=NATURAL_MAX_TOOL_CALLS,
        max_input_tokens=NATURAL_MAX_INPUT_TOKENS,
        max_output_tokens=NATURAL_MAX_OUTPUT_TOKENS,
    )
    if strategy is Strategy.REACT:
        return base
    return base.model_copy(update={"max_steps": base.max_steps + PLANNER_STEPS})


def whole_run_budget(case: AgentCase, strategy: Strategy) -> RunBudget:
    """Return the equal-resource budget: one shared whole-run ceiling.

    Both arms receive identical limits and Plan-Execute gets **no** planner
    grant, so its planning decision is paid for out of the same allowance ReAct
    uses for tool calls. Any resulting capacity loss is a real property of the
    strategy under equal resources, not an artifact of the harness.
    """
    del case, strategy
    return RunBudget(
        max_steps=EQUAL_TOTAL_RUN_STEPS,
        max_tool_calls=EQUAL_MAX_TOOL_CALLS,
        max_input_tokens=EQUAL_MAX_INPUT_TOKENS,
        max_output_tokens=EQUAL_MAX_OUTPUT_TOKENS,
    )


def live_budget(case: AgentCase, strategy: Strategy, profile: Profile) -> RunBudget:
    """Select the budget a live profile applies.

    A case's own ``budget`` override is a keyless artifact, calibrated against
    that specific ceiling; applying it in a live profile would silently change
    what the profile means, so the profile budget wins.
    """
    if profile is Profile.EQUAL:
        return whole_run_budget(case, strategy)
    if profile is Profile.NATURAL:
        return natural_cost_budget(case, strategy)
    raise ValueError(f"live_budget requires a live profile, got {profile.value}")


def _build_runtime(
    case: AgentCase,
    strategy: Strategy,
    trace: InMemoryTrace,
    *,
    profile: Profile = Profile.KEYLESS,
    model_factory: ModelFactory | None = None,
) -> AgentRuntime | PlanExecuteRuntime:
    """Compose one arm for one case under the requested profile."""
    registry = _build_registry(case)
    if model_factory is None:
        executor_model: Model = ScriptedModel(_scripted_responses(case))
        planner_model: Model | None = ScriptedModel(
            [
                ModelResult(
                    action=FinalAction(content=json.dumps({"steps": _plan_steps(case)})),
                    usage=ModelUsage(input_tokens=7, output_tokens=3),
                    model_name="scripted-planner",
                )
            ]
        )
        budget = executor_step_budget(case, strategy)
    else:
        executor_model = model_factory()
        planner_model = model_factory()
        budget = live_budget(case, strategy, profile)
    executor = AgentRuntime(
        model=executor_model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAllPolicy(),
        trace=trace,
        budget=budget,
    )
    if strategy is Strategy.REACT:
        return executor
    if planner_model is None:
        raise AssertionError("plan-execute requires a planner model")
    return PlanExecuteRuntime(
        planner=planner_model,
        executor=executor,
        trace=trace,
        max_planner_retries=RunBudget().max_planner_retries,
    )


def _observed(result: RunResult, trace: InMemoryTrace, latency_ms: float) -> ObservedRun:
    """Read cumulative usage from the result and call facts from the projection.

    Step facts come from `observability.steps`, the project's single trace→step
    projection, so the evaluator and `forge trace-steps` can never disagree about
    what happened. Usage comes from `RunResult.usage` rather than being summed
    from events, so a Plan-Execute run's planner cost lands in the same fields as
    a ReAct run's model cost; the contract checks assert that stays true.
    """
    projection = project_events(trace.events)
    error: str | None = None
    for step in projection.steps:
        if step.error_type is not None:
            error = step.error_type
            break
    return ObservedRun(
        status=result.status,
        tool_sequence=projection.tool_sequence,
        tool_arguments=projection.tool_arguments,
        plan_events=projection.plan_events,
        planner_requests=projection.planner_requests,
        steps=result.usage.steps,
        model_decisions=projection.model_decisions,
        logical_tool_calls=result.usage.tool_calls,
        tool_attempts=projection.tool_attempts,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        latency_ms=latency_ms,
        error=error or result.error,
    )


def _set_accuracy(actual: tuple[str, ...], required: tuple[str, ...], no_tool: bool) -> float:
    actual_set = set(actual)
    required_set = set(required)
    if no_tool:
        return 1.0 if not actual_set else 0.0
    if not actual_set and not required_set:
        return 1.0
    union = actual_set | required_set
    return len(actual_set & required_set) / len(union)


def _argument_accuracy(
    actual_names: tuple[str, ...],
    actual_args: tuple[dict[str, Any], ...],
    expected_names: tuple[str, ...] | None,
    constraints: tuple[dict[str, Any] | None, ...] | None,
) -> float | None:
    if constraints is None or expected_names is None:
        return None
    total = 0
    matched = 0
    for position, constraint in enumerate(constraints):
        if constraint is None:
            continue
        total += 1
        if position >= len(actual_names):
            continue
        if actual_names[position] != expected_names[position]:
            continue
        supplied = actual_args[position]
        if all(supplied.get(key) == value for key, value in constraint.items()):
            matched += 1
    return 1.0 if total == 0 else matched / total


def judge_case(case: AgentCase, observed: ObservedRun, *, planner_steps: int = 0) -> CaseJudgement:
    """Apply the case's hard assertions, then record continuous metrics.

    ``args_contains`` is a hard assertion as well as a continuous metric: when a
    case pins arguments it must satisfy them to pass. Without that, a run with
    the right tools and wrong arguments would be scored as a success.

    ``planner_steps`` offsets the step ceiling for runtimes that spend decisions
    before the executor loop. Case contracts describe executor-phase expectations
    ("this task should take ≤N decisions to act"), and planning is a fixed
    structural cost of a strategy rather than an acting choice, so the offset
    applies in every profile. Resource fairness is a property of the shared
    budget, not of this assertion: under equal resources a truncated run reports
    ``EXHAUSTED`` and fails the status assertion by itself.
    """
    expected = case.expected
    failures: list[str] = []
    if observed.status != expected.status:
        failures.append(f"status:{observed.status.value}!=expected:{expected.status.value}")
    forbidden = sorted(set(observed.tool_sequence) & set(expected.forbidden_tools))
    if forbidden:
        failures.append(f"forbidden_tools:{','.join(forbidden)}")
    missing = sorted(set(expected.tools) - set(observed.tool_sequence))
    if missing:
        failures.append(f"missing_tools:{','.join(missing)}")
    if (
        expected.max_tool_calls is not None
        and observed.logical_tool_calls > expected.max_tool_calls
    ):
        failures.append(f"tool_calls:{observed.logical_tool_calls}>max:{expected.max_tool_calls}")
    if expected.no_tool and observed.tool_sequence:
        failures.append(f"unexpected_tools:{','.join(sorted(set(observed.tool_sequence)))}")
    if expected.tool_sequence is not None and observed.tool_sequence != expected.tool_sequence:
        failures.append(
            f"sequence:{'/'.join(observed.tool_sequence)}!=expected:{'/'.join(expected.tool_sequence)}"
        )
    step_ceiling = None if expected.max_steps is None else expected.max_steps + planner_steps
    if step_ceiling is not None and observed.steps > step_ceiling:
        failures.append(f"steps:{observed.steps}>max:{step_ceiling}")
    metrics = CaseMetrics(
        tool_set_accuracy=_set_accuracy(observed.tool_sequence, expected.tools, expected.no_tool),
        tool_sequence_match=(
            None
            if expected.tool_sequence is None
            else float(observed.tool_sequence == expected.tool_sequence)
        ),
        argument_accuracy=_argument_accuracy(
            observed.tool_sequence,
            observed.tool_arguments,
            expected.tool_sequence,
            expected.args_contains,
        ),
        forbidden_violation=bool(forbidden),
    )
    if metrics.argument_accuracy is not None and metrics.argument_accuracy < 1.0:
        failures.append(f"arguments:accuracy={metrics.argument_accuracy:.3f}")
    return CaseJudgement(passed=not failures, failed_assertions=tuple(failures), metrics=metrics)


def contract_checks(
    case: AgentCase,
    observed: ObservedRun,
    strategy: Strategy,
    *,
    live: bool = False,
    max_planner_retries: int = 1,
) -> tuple[ContractCheck, ...]:
    """Check evaluator-wiring invariants that hold independently of pass rate.

    These validate the wiring, not agent quality:

    * executed calls must be a prefix of the scripted decisions, because a budget
      gate may stop the run part-way through the script. On a live run there is
      no script to compare against, so this check is omitted rather than faked;
    * every admitted call must have been executed — the benchmark registry
      contains no denied or unknown tools, so any gap means counting and dispatch
      disagree;
    * a Plan-Execute run must invoke its planner at least once, and no more than
      its retry budget allows (one attempt plus bounded corrections); a ReAct run
      must never invoke it. That is how the evaluator proves it drove the runtime
      it claims to have driven. The check counts planner *invocations*, not valid
      plans: a live planner that answers with prose is a quality failure the
      scoring already records, and must not also be reported as a broken harness.
    """
    admitted_ok = observed.logical_tool_calls == observed.tool_attempts
    if strategy is Strategy.PLAN_EXECUTE:
        planned_ok = 1 <= observed.planner_requests <= 1 + max_planner_retries
        planned_detail = "" if planned_ok else f"planner_requests={observed.planner_requests}"
        planner_counted = observed.steps >= PLANNER_STEPS
        planner_detail = "" if planner_counted else f"steps={observed.steps}"
    else:
        planned_ok = observed.planner_requests == 0
        planned_detail = (
            "" if planned_ok else f"unexpected planner_requests={observed.planner_requests}"
        )
        planner_counted = True
        planner_detail = ""
    checks: list[ContractCheck] = []
    if not live:
        scripted = tuple(step.tool for step in case.script if step.tool is not None)
        executed = observed.tool_sequence
        prefix_ok = executed == scripted[: len(executed)]
        checks.append(
            ContractCheck(
                name=f"{case.id}:executed_calls_are_script_prefix",
                passed=prefix_ok,
                detail="" if prefix_ok else f"executed={'/'.join(executed)}",
            )
        )
    checks.extend(
        [
            ContractCheck(
                name=f"{case.id}:admitted_calls_were_executed",
                passed=admitted_ok,
                detail=(
                    ""
                    if admitted_ok
                    else f"admitted={observed.logical_tool_calls},attempts={observed.tool_attempts}"
                ),
            ),
            ContractCheck(
                name=f"{case.id}:planner_lifecycle", passed=planned_ok, detail=planned_detail
            ),
        ]
    )
    if strategy is Strategy.PLAN_EXECUTE:
        checks.append(
            ContractCheck(
                name=f"{case.id}:planner_step_counted",
                passed=planner_counted,
                detail=planner_detail,
            )
        )
    return tuple(checks)


async def run_case(
    case: AgentCase,
    strategy: Strategy,
    workspace: Path,
    *,
    profile: Profile = Profile.KEYLESS,
    model_factory: ModelFactory | None = None,
) -> tuple[ObservedRun, CaseJudgement, tuple[ContractCheck, ...], tuple[TraceEvent, ...]]:
    """Execute one case in an isolated workspace and judge the result.

    With a ``model_factory`` the case runs against a real model under the chosen
    live profile; without one it runs keylessly against the scripted decisions.
    """
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    for relative, content in case.workspace.items():
        target = workspace / relative
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
    trace = InMemoryTrace(case.id)
    runtime = _build_runtime(case, strategy, trace, profile=profile, model_factory=model_factory)
    live = model_factory is not None
    # A case's step ceiling describes the *executor phase*. Planning is a fixed
    # structural cost of the strategy, so the planner decision is offset in every
    # profile; without it a `max_steps=1` no-tool case could never be satisfied by
    # Plan-Execute regardless of behaviour, which would score the strategy on
    # having a planner rather than on acting well. The equal-resource penalty is
    # expressed through the shared budget instead (a truncated run reports
    # EXHAUSTED and fails its status assertion on its own merits).
    planner_steps = PLANNER_STEPS if strategy is Strategy.PLAN_EXECUTE else 0
    started = perf_counter()
    result = await runtime.run(task_id=case.id, task=case.task, workspace=workspace)
    observed = _observed(result, trace, round((perf_counter() - started) * 1000, 3))
    judgement = judge_case(case, observed, planner_steps=planner_steps)
    # The raw events are returned so a failing sample can be persisted as
    # evidence. Without them a Bad Case would be a detached summary rather than
    # something that points back at what actually happened.
    return (
        observed,
        judgement,
        contract_checks(
            case,
            observed,
            strategy,
            live=live,
            max_planner_retries=RunBudget().max_planner_retries,
        ),
        tuple(trace.events),
    )


# A profile that claims to constrain resources must actually constrain them. Below
# this share of runs hitting the ceiling, the equal-resource profile is
# indistinguishable from the natural-cost one and may not be used for
# resource-limited conclusions.
MIN_CONSTRAINED_RUN_SHARE = 0.10


def holdout_ids_hash(manifest: AgentCaseManifest) -> str:
    """Hash the frozen holdout set's identity, independent of dev-case edits."""
    ids = sorted(case.id for case in manifest.for_split("holdout"))
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


class FreezeManifest(FrozenModel):
    """What a final evaluation's numbers are bound to.

    Written once before the holdout run and referenced by every report from then on,
    so a figure can always be traced to the code revision, the case set, and the
    runtime semantics that produced it. Every field is read from live constants
    rather than typed by hand, so the manifest cannot drift from the code.
    """

    created_at: datetime
    git_revision: str
    model_name: str
    model_parameters: dict[str, Any]
    benchmark_version: int
    observation_contract_version: int
    recovery_semantics_version: int
    budget_profile_version: int
    holdout_case_ids_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    holdout_cases: int = Field(ge=0)
    samples_per_case: int = Field(ge=1)
    natural_budget: dict[str, int]
    equal_step_cap: int = Field(ge=1)
    notes: str = ""


async def build_freeze_manifest(
    *, manifest_path: Path, project_root: Path, model_name: str, samples: int
) -> FreezeManifest:
    """Build the freeze manifest from live constants and the checked-in case set."""
    manifest = AgentCaseManifest.model_validate_json(
        await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
    )
    # The natural budget is a constant per strategy; any case yields it.
    natural = natural_cost_budget(manifest.cases[0], Strategy.REACT)
    return FreezeManifest(
        created_at=datetime.now(UTC),
        git_revision=await _revision(project_root),
        model_name=model_name,
        model_parameters=live_eval_config(
            base_url="http://127.0.0.1:8000/v1", api_key="omlx-local", model=model_name
        ).model_dump(mode="json"),
        benchmark_version=BENCHMARK_VERSION,
        observation_contract_version=OBSERVATION_CONTRACT_VERSION,
        recovery_semantics_version=RECOVERY_SEMANTICS_VERSION,
        budget_profile_version=BUDGET_PROFILE_VERSION,
        holdout_case_ids_hash=holdout_ids_hash(manifest),
        holdout_cases=len(manifest.for_split("holdout")),
        samples_per_case=samples,
        natural_budget={
            "max_steps": natural.max_steps,
            "max_tool_calls": natural.max_tool_calls,
            "max_input_tokens": natural.max_input_tokens,
            "max_output_tokens": natural.max_output_tokens,
        },
        equal_step_cap=EQUAL_TOTAL_RUN_STEPS,
        notes=(
            "Frozen before the final holdout evaluation. No prompt, planner, parser, "
            "observation-format, retry, or deadline change is permitted while these "
            "numbers are the reported ones; a further change requires a new holdout set."
        ),
    )


def constraint_binding(
    *, budgets: dict[str, dict[str, dict[str, int]]], outcomes: tuple[CaseOutcome, ...]
) -> tuple[bool | None, float | None]:
    """Report whether the applied step ceiling actually constrained any run.

    Only meaningful when a step ceiling is the intended binding constraint; the
    caller passes ``None`` for profiles that do not claim one.
    """
    ceilings = {
        case_id: values["max_steps"]
        for case_values in budgets.values()
        for case_id, values in case_values.items()
    }
    if not ceilings:
        return None, None
    constrained = sum(
        1 for outcome in outcomes if outcome.steps >= ceilings.get(outcome.id, 1 << 30)
    )
    share = constrained / len(outcomes) if outcomes else 0.0
    return share >= MIN_CONSTRAINED_RUN_SHARE, share


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def review_floor(benchmark_cases: int, categories_covered: bool, holdout_sufficient: bool) -> bool:
    """Structural conditions every benchmark report must satisfy.

    These gate dataset and evaluator integrity, not agent quality: a report is
    structurally usable only when it graded at least one benchmark case, covered
    every category, and the manifest still has an adequate holdout split.
    """
    return benchmark_cases >= 1 and categories_covered and holdout_sufficient


def _optional_mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return _mean(present) if present else None


def _arm_summary(
    strategy: Strategy, outcomes: tuple[CaseOutcome, ...], *, is_keyless: bool
) -> ArmSummary:
    selected = [outcome for outcome in outcomes if outcome.strategy is strategy]
    benchmark = [outcome for outcome in selected if not outcome.is_probe]
    probes = [outcome for outcome in selected if outcome.is_probe]
    detected = sum(1 for outcome in probes if not outcome.judged_pass)
    return ArmSummary(
        strategy=strategy,
        benchmark_cases=len(benchmark),
        benchmark_pass=sum(outcome.judged_pass for outcome in benchmark),
        mean_pass_rate=_mean([outcome.pass_rate for outcome in benchmark]),
        scorer_probes=len(probes),
        scorer_probes_detected=detected,
        probe_detection_rate=(detected / len(probes)) if probes else 1.0,
        contract_checks_passed=True,
        contract_failures=(),
        tool_set_accuracy=_mean([outcome.tool_set_accuracy for outcome in benchmark]),
        tool_sequence_match=_optional_mean([outcome.tool_sequence_match for outcome in benchmark]),
        argument_accuracy=_optional_mean([outcome.argument_accuracy for outcome in benchmark]),
        mean_steps=_mean([float(outcome.steps) for outcome in benchmark]),
        mean_logical_tool_calls=_mean([float(outcome.logical_tool_calls) for outcome in benchmark]),
        diagnostics=PipelineDiagnostics(
            synthetic_token_usage=is_keyless,
            mean_input_tokens=_mean([float(outcome.input_tokens) for outcome in benchmark]),
            mean_output_tokens=_mean([float(outcome.output_tokens) for outcome in benchmark]),
            mean_tool_attempts=_mean([float(outcome.tool_attempts) for outcome in benchmark]),
        ),
    )


def _category_summary(
    strategy: Strategy, category: CaseCategory, outcomes: tuple[CaseOutcome, ...]
) -> CategorySummary:
    selected = [
        outcome
        for outcome in outcomes
        if outcome.category is category and outcome.strategy is strategy and not outcome.is_probe
    ]
    return CategorySummary(
        strategy=strategy,
        category=category,
        cases=len(selected),
        judged_pass=sum(outcome.judged_pass for outcome in selected),
        tool_set_accuracy=_mean([outcome.tool_set_accuracy for outcome in selected]),
        argument_accuracy=_optional_mean([outcome.argument_accuracy for outcome in selected]),
    )


async def _run_one_case(
    case: AgentCase,
    strategy: Strategy,
    *,
    profile: Profile,
    samples: int,
    work_root: Path,
    model_factory: ModelFactory | None,
) -> tuple[CaseOutcome, tuple[ContractCheck, ...], CaseFailureEvidence | None]:
    """Run one case ``samples`` times and reduce the samples to one outcome.

    ``judged_pass`` requires **every** sample to pass, so a flaky solve is not
    counted as a success; ``pass_rate`` keeps the partial credit visible. Numeric
    metrics are the per-sample median rather than the mean of a mixed set, so a
    single outlier cannot drag the reported cost around.

    The first failing sample is returned as evidence so it can be persisted. Only
    the first is kept: later samples usually repeat the same failure, and one
    traceable instance is what a Bad Case needs.
    """
    samples_seen: list[tuple[ObservedRun, CaseJudgement]] = []
    checks: list[ContractCheck] = []
    failure: CaseFailureEvidence | None = None
    for sample in range(samples):
        # One workspace per (strategy, case, sample): write tools mutate files, so
        # neither arms nor repeats may share a fixture.
        workspace = work_root / f"{strategy.value}-{case.id}-{sample}"
        observed, judgement, sample_checks, events = await run_case(
            case, strategy, workspace, profile=profile, model_factory=model_factory
        )
        samples_seen.append((observed, judgement))
        checks.extend(sample_checks)
        if failure is None and not judgement.passed:
            failure = CaseFailureEvidence(
                sample_index=sample, observed=observed, judgement=judgement, events=events
            )
    passes = sum(1 for _, judgement in samples_seen if judgement.passed)
    representative = samples_seen[-1][1]
    observed_median = _median_observed(samples_seen)
    failed_assertions = (
        ()
        if passes == samples
        else tuple(
            sorted({item for _, judgement in samples_seen for item in judgement.failed_assertions})
        )
    )
    outcome = CaseOutcome(
        id=case.id,
        strategy=strategy,
        category=case.category,
        split=case.split,
        is_probe=case.is_probe,
        expected_grade=case.grade,
        no_tool_required=case.expected.no_tool,
        judged_pass=passes == samples,
        agreed=(passes == samples) == (case.grade == "pass"),
        failed_assertions=failed_assertions,
        samples=samples,
        passes=passes,
        pass_rate=passes / samples,
        tool_set_accuracy=representative.metrics.tool_set_accuracy,
        tool_sequence_match=representative.metrics.tool_sequence_match,
        argument_accuracy=representative.metrics.argument_accuracy,
        forbidden_violation=any(
            judgement.metrics.forbidden_violation for _, judgement in samples_seen
        ),
        status=observed_median.status,
        steps=observed_median.steps,
        logical_tool_calls=observed_median.logical_tool_calls,
        tool_attempts=observed_median.tool_attempts,
        input_tokens=observed_median.input_tokens,
        output_tokens=observed_median.output_tokens,
        latency_ms=observed_median.latency_ms,
    )
    return outcome, tuple(checks), failure


def _median_observed(samples: list[tuple[ObservedRun, CaseJudgement]]) -> ObservedRun:
    """Return the sample whose step count is the median, for stable reporting."""
    ordered = sorted(samples, key=lambda item: item[0].steps)
    return ordered[len(ordered) // 2][0]


def _model_boundary_evidence(events: tuple[TraceEvent, ...], actual_status: str) -> FailureEvidence:
    """Extract the model-boundary facts a failure classification is based on.

    Reads the stable `error_code` when the provider supplied one, so the
    classification does not depend on matching a human-readable message.

    A boundary failure is only treated as the *cause* when the run did not
    recover. Since protocol violations can now be corrected, a `model.failed` or
    `plan.failed` event may be followed by a successful run; attributing that run
    to the boundary failure would mislabel a recovered execution as a harness
    defect.
    """
    recovered = actual_status == "succeeded"
    if not recovered:
        for event in events:
            if event.type in {"model.failed", "verification.failed", "plan.failed"}:
                payload = event.payload
                code = payload.get("error_code")
                count = payload.get("received_tool_calls")
                return FailureEvidence(
                    actual_status=actual_status,
                    error_code=str(code) if code is not None else None,
                    error_type=str(payload.get("error_type", event.type)),
                    error_message=str(payload.get("error", "")),
                    received_tool_calls=count if isinstance(count, int) else None,
                )
    return FailureEvidence(actual_status=actual_status)


def bad_case_artifact_key(
    *, profile: Profile, strategy: Strategy, case_id: str, sample_id: int
) -> str:
    """Return the artifact identity shared by a Bad Case and its trace.

    `profile` and `sample_id` are part of the key, not only of the record body:
    the same case under `equal` and `natural` can behave differently, and repeated
    live sampling can fail on one sample and not another. Omitting either would
    silently overwrite an earlier capture, which matters because a Bad Case is
    evidence and evidence that overwrites itself is not evidence.
    """
    return f"{profile.value}-{strategy.value}-{case_id}-s{sample_id}"


def _repeated_tool_error(events: list[TraceEvent]) -> tuple[str, int] | None:
    """Return a tool error that recurred with the same text, and its count.

    Distinct from a no-progress loop: the *arguments* may differ (the model kept
    guessing), so this is repetitive failure rather than a repeated identical call.
    """
    seen: dict[str, int] = {}
    for step in project_events(events).steps:
        if step.action_kind is not StepActionKind.TOOL or step.tool_ok is not False:
            continue
        error = (step.observation or "").strip()
        if not error:
            continue
        seen[error] = seen.get(error, 0) + 1
    for error, count in seen.items():
        if count >= 2:
            return error, count
    return None


async def _capture_bad_case(
    *,
    case: AgentCase,
    strategy: Strategy,
    profile: Profile,
    model_name: str,
    revision: str,
    failure: CaseFailureEvidence,
    bad_case_root: Path,
) -> BadCase | None:
    """Persist one failing sample as a Bad Case bound to its trace evidence.

    Returns ``None`` for a failure that needs no intervention: a pure
    ``model_quality`` result with no harness gap and no case-design weakness is
    the model doing badly, not a defect to fix. It is already recorded in the
    report (assertions and metrics) and reproduces on any re-run, whereas the Bad
    Case directory should stay a list of things to act on — otherwise a full
    regression adds ~30 entries and buries the actionable ones.

    Artifacts are written under `bad_case_root`, derived from the report's own
    output location rather than the repository root. Writing to a fixed repo path
    would make every test run and every exploratory evaluation pollute the working
    tree.
    """
    observed = failure.observed
    evidence = _model_boundary_evidence(failure.events, observed.status.value)
    evidence = evidence.model_copy(
        update={
            "failed_assertions": failure.judgement.failed_assertions,
            "expected_max_tool_calls": case.expected.max_tool_calls,
            "actual_tool_calls": observed.logical_tool_calls,
        }
    )
    failure_class, failure_point, diagnosis, harness_gap = classify_failure(evidence)
    projection = project_events(failure.events)
    repeated, no_progress_signature = no_progress_diagnosis(list(failure.events))
    # A second, distinct harness gap: the same tool kept failing with the same raw
    # exception, which reaches the model as an unstructured Python string rather
    # than as actionable feedback it could recover from.
    repeated_tool_error = _repeated_tool_error(list(failure.events))
    if harness_gap is None and repeated_tool_error is not None:
        harness_gap = (
            f"the same tool error recurred {repeated_tool_error[1]} times and is surfaced "
            "as a raw exception string rather than an actionable observation the model "
            "could recover from"
        )
    case_design_note = fixture_gap_note(tuple(case.workspace), list(failure.events))
    if (
        failure_class is FailureClass.MODEL_QUALITY
        and harness_gap is None
        and case_design_note is None
    ):
        # The model simply did the task badly. Nothing for the harness to act on,
        # and the report already carries the evidence.
        return None
    artifact_key = bad_case_artifact_key(
        profile=profile, strategy=strategy, case_id=case.id, sample_id=failure.sample_index
    )
    trace_name = f"traces/{artifact_key}.jsonl"
    bad_case_path = bad_case_root / f"{artifact_key}.json"
    trace_path = bad_case_root / trace_name
    trace_hash, _ = await asyncio.to_thread(
        persist_trace, list(failure.events), trace_path, task_id=case.id
    )
    executor_script, planner_script = _replay_scripts(failure.events, strategy)
    bad_case = BadCase(
        case_id=case.id,
        arm=strategy.value,
        revision=revision,
        profile=profile.value,
        model=model_name,
        sample_id=failure.sample_index,
        trace_path=str(trace_name),
        trace_hash=trace_hash,
        expected_status=case.expected.status.value,
        expected_tools=case.expected.tools,
        expected_sequence=case.expected.tool_sequence,
        actual_status=observed.status.value,
        actual_sequence=observed.tool_sequence,
        failed_assertions=failure.judgement.failed_assertions,
        steps=observed.steps,
        logical_tool_calls=observed.logical_tool_calls,
        tool_attempts=observed.tool_attempts,
        projected_steps=projected_step_summary(list(failure.events)),
        model_decisions=projection.model_decisions,
        model_requests=projection.model_requests,
        usage_steps=observed.steps,
        received_tool_calls=evidence.received_tool_calls,
        failure_class=failure_class,
        failure_point=failure_point,
        error_type=evidence.error_code or evidence.error_type,
        diagnosis=diagnosis,
        harness_gap=harness_gap,
        case_design_note=case_design_note,
        replay_eligibility=ReplayEligibility.DETERMINISTIC,
        # Capture only this round: the recorded channel is a declared candidate,
        # not an implemented capability.
        replay_mode=ReplayMode.RECORDED_MODEL_CANDIDATE,
        replay_note=(
            "Recorded replay is a candidate for this case and is not implemented yet. "
            "It will reproduce harness behaviour with the model's choices held "
            "constant; it cannot show that a model or prompt change helped, which "
            "requires a live re-evaluation."
        ),
        planner_script=planner_script,
        executor_script=executor_script or (),
        repeated_calls=repeated,
        repeated_call_signature=no_progress_signature,
        repeat_count=(
            next(item.repeat_count for item in repeated if item.signature == no_progress_signature)
            if no_progress_signature is not None
            else None
        ),
        observation_hashes=(
            next(
                item.observation_hashes
                for item in repeated
                if item.signature == no_progress_signature
            )
            if no_progress_signature is not None
            else ()
        ),
    )
    await asyncio.to_thread(write_bad_case, bad_case, bad_case_path)
    return bad_case


def _replay_scripts(
    events: tuple[TraceEvent, ...], strategy: Strategy
) -> tuple[tuple[ReplayStep, ...], tuple[ReplayStep, ...] | None]:
    """Split recorded model outcomes into executor and planner scripts.

    Each script comes from its own event source, so the two can never be the same
    list: executor decisions arrive as `model.action`, while the planner's outcome
    is recorded on `plan.created`.
    """
    executor = executor_replay_script(list(events))
    if strategy is not Strategy.PLAN_EXECUTE:
        return executor, None
    return executor, planner_replay_script(list(events))


async def run_agent_evaluation(
    *,
    manifest_path: Path,
    output_path: Path,
    project_root: Path,
    split: Split = "dev",
    strategies: tuple[Strategy, ...] = (Strategy.REACT, Strategy.PLAN_EXECUTE),
    profile: Profile = Profile.KEYLESS,
    model_factory: ModelFactory | None = None,
    model_name: str = "scripted",
    samples: int = 1,
    limit: int | None = None,
    only_cases: tuple[str, ...] = (),
    model_config: dict[str, Any] | None = None,
) -> AgentEvaluationReport:
    """Run one split across the requested runtimes and persist one JSON report.

    ``KEYLESS`` validates the grading contract, the metric pipeline, and the
    wiring of both runtimes; it cannot compare strategies, because a scripted
    model replaces the decision under test. ``EQUAL`` and ``NATURAL`` run a real
    model through ``model_factory`` and are the only profiles whose numbers may
    be used to compare orchestration strategies.
    """
    if split not in {"dev", "holdout"}:
        raise ValueError(f"unsupported split: {split}")
    if not strategies:
        raise ValueError("at least one strategy is required")
    if samples < 1:
        raise ValueError("samples must be at least 1")
    is_keyless = profile is Profile.KEYLESS
    if is_keyless and model_factory is not None:
        raise ValueError("the keyless profile must not be given a live model factory")
    if not is_keyless:
        if model_factory is None:
            raise ValueError(f"the {profile.value} profile requires a live model factory")
        if samples < 2:
            raise ValueError(
                f"the {profile.value} profile requires repeated sampling: a single "
                "live sample is not reproducible and cannot support a comparison"
            )
    manifest_bytes = await asyncio.to_thread(manifest_path.read_bytes)
    manifest = AgentCaseManifest.model_validate_json(manifest_bytes)
    cases = manifest.for_split(split)
    if not cases:
        raise ValueError(f"manifest has no '{split}' cases")
    if not is_keyless:
        # Live profiles run quality cases only. Scorer probes are negative
        # controls that validate the evaluator itself, and `budget_boundary` cases
        # assert harness-mechanism outcomes under a per-case budget the live
        # profiles deliberately override; both are unsatisfiable or meaningless
        # against a real model, and including them would dilute the comparison
        # with always-failing cases.
        cases = tuple(
            case
            for case in cases
            if not case.is_probe and case.category not in KEYLESS_ONLY_CATEGORIES
        )
    if limit is not None:
        # Chunking a long run is a practical necessity on a local model server:
        # hundreds of sequential inferences accumulate memory, and a bounded run
        # can be interrupted and resumed without losing the host.
        if limit < 1:
            raise ValueError("limit must be at least 1")
        cases = cases[:limit]
    if only_cases:
        # Diagnosing one failure should not require re-running the whole split;
        # `retr-find-then-read` and friends are cheap to isolate.
        unknown = set(only_cases) - {case.id for case in manifest.cases}
        if unknown:
            raise ValueError(f"unknown case ids: {sorted(unknown)}")
        cases = tuple(case for case in cases if case.id in set(only_cases))
        if not cases:
            raise ValueError("only_cases selected no runnable case")
    work_root = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="forgeharness-agent-"))
    collected: list[tuple[CaseOutcome, Strategy, tuple[ContractCheck, ...]]] = []
    bad_cases: list[BadCase] = []
    # Trace evidence is written against the revision the run is bound to, so a
    # Bad Case can point at exactly the source that produced it.
    revision = await _revision(project_root)
    evaluation_started = perf_counter()
    try:
        for strategy in strategies:
            for case in cases:
                outcome, checks, failure = await _run_one_case(
                    case,
                    strategy,
                    profile=profile,
                    samples=samples,
                    work_root=work_root,
                    model_factory=model_factory,
                )
                collected.append((outcome, strategy, checks))
                # A scorer probe is *designed* to fail: it validates the evaluator
                # itself, so capturing one as a Bad Case would flood the evidence
                # directory with entries describing no real defect. Only an
                # unexpected failure is evidence.
                if failure is not None and not case.is_probe:
                    captured = await _capture_bad_case(
                        case=case,
                        strategy=strategy,
                        profile=profile,
                        model_name=model_name,
                        revision=revision,
                        failure=failure,
                        bad_case_root=output_path.parent / "bad-cases",
                    )
                    # `None` means the failure needs no intervention, so no
                    # artifact is written for it.
                    if captured is not None:
                        bad_cases.append(captured)
    finally:
        await asyncio.to_thread(shutil.rmtree, work_root, True)
    wall_time_ms = round((perf_counter() - evaluation_started) * 1000, 3)
    outcomes = tuple(outcome for outcome, _, _ in collected)
    arms: list[ArmSummary] = []
    for strategy in strategies:
        failures = tuple(
            check.name
            for _, arm_strategy, checks in collected
            if arm_strategy is strategy
            for check in checks
            if not check.passed
        )
        arms.append(
            _arm_summary(strategy, outcomes, is_keyless=is_keyless).model_copy(
                update={
                    "contract_checks_passed": not failures,
                    "contract_failures": failures,
                }
            )
        )
    arms_tuple = tuple(arms)
    benchmark_cases = sum(1 for case in cases if not case.is_probe)
    scorer_probes = sum(1 for case in cases if case.is_probe)
    # Scorer validity is only measurable where probes exist. Reporting a vacuous
    # 1.0 on a probe-free split would read as "perfect agreement" when nothing
    # was checked, so the field is absent instead.
    grading_agreement = (
        _mean([arm.probe_detection_rate for arm in arms_tuple]) if scorer_probes else None
    )
    categories_covered = all(
        any(case.category is category for case in cases)
        for category in set(CaseCategory) - KEYLESS_ONLY_CATEGORIES
    )
    holdout_sufficient = len(manifest.for_split("holdout")) >= manifest.min_holdout
    complete_split = limit is None and not only_cases
    contract_ok = all(arm.contract_checks_passed for arm in arms_tuple)
    # Measurement validity is profile-independent: it says the experiment's data
    # is usable, never that the model did well. A truncated run is exploratory,
    # so it is never usable as a measurement.
    measurement_valid = (
        complete_split
        and contract_ok
        and review_floor(benchmark_cases, categories_covered, holdout_sufficient)
    )
    if is_keyless:
        # A scripted model replays a known-good script, so any benchmark failure
        # means the evaluator or the runtime wiring is wrong, and every probe
        # must be caught. Both are hard gate conditions.
        react_all_pass = all(
            arm.benchmark_pass == arm.benchmark_cases
            for arm in arms_tuple
            if arm.strategy is Strategy.REACT
        )
        keyless_gate_passed: bool | None = (
            measurement_valid and react_all_pass and grading_agreement == 1.0
        )
    else:
        # A live-model run legitimately produces failed cases; failing them is a
        # quality result, not a broken gate. The keyless gate does not apply.
        keyless_gate_passed = None
    thresholds = {
        "grading_agreement": 1.0,
        "min_holdout_cases": float(manifest.min_holdout),
        "min_categories": float(len(CaseCategory)),
        "react_benchmark_all_pass": 1.0,
    }
    budget_record = _budget_record(cases, strategies, profile)
    holdout_case_ids_hash = holdout_ids_hash(manifest)
    # Only the equal-resource profile claims a binding constraint; the others run
    # unbounded by design, so the check does not apply to them.
    if profile is Profile.EQUAL:
        constraint_effective, constrained_run_share = constraint_binding(
            budgets=budget_record, outcomes=outcomes
        )
    else:
        constraint_effective, constrained_run_share = None, None
    report = AgentEvaluationReport(
        schema_version=manifest.schema_version,
        created_at=datetime.now(UTC),
        revision=await _revision(project_root),
        profile=profile,
        model_name=model_name,
        split=split,
        token_source="synthetic" if is_keyless else "model_reported",
        cost_metrics_valid=not is_keyless,
        budgets=budget_record,
        samples_per_case=samples,
        model_parameters=model_config or {},
        constraint_effective=constraint_effective,
        constrained_run_share=constrained_run_share,
        complete_split=complete_split,
        wall_time_ms=wall_time_ms,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        holdout_case_ids_hash=holdout_case_ids_hash,
        manifest_split_counts={name: len(manifest.for_split(name)) for name in ("dev", "holdout")},
        benchmark_cases=benchmark_cases,
        scorer_probes=scorer_probes,
        grading_agreement=grading_agreement,
        measurement_valid=measurement_valid,
        keyless_gate_passed=keyless_gate_passed,
        thresholds=thresholds,
        arms=arms_tuple,
        categories=tuple(
            _category_summary(strategy, category, outcomes)
            for strategy in strategies
            for category in CaseCategory
        ),
        cases=outcomes,
    )
    await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        output_path.write_text, report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return report


def _budget_record(
    cases: tuple[AgentCase, ...], strategies: tuple[Strategy, ...], profile: Profile
) -> dict[str, dict[str, dict[str, int]]]:
    """Record the whole-run budget each arm received, per case.

    Two profiles can otherwise be confused after the fact: this makes explicit
    what ceiling each arm was actually under, so an equal-resource report cannot
    be mistaken for a natural-cost one.
    """
    record: dict[str, dict[str, dict[str, int]]] = {}
    for strategy in strategies:
        per_case: dict[str, dict[str, int]] = {}
        for case in cases:
            if profile is Profile.KEYLESS:
                budget = executor_step_budget(case, strategy)
            else:
                budget = live_budget(case, strategy, profile)
            per_case[case.id] = {
                "max_steps": budget.max_steps,
                "max_tool_calls": budget.max_tool_calls,
                "max_input_tokens": budget.max_input_tokens,
                "max_output_tokens": budget.max_output_tokens,
            }
        record[strategy.value] = per_case
    return record


def omlx_model_factory(config: OpenAICompatibleConfig, client: httpx.AsyncClient) -> ModelFactory:
    """Return a factory that makes a stateless model sharing one HTTP client.

    The returned factory may be called many times; every model reuses `client`, so
    the caller keeps ownership of the connection pool and closes it once. The
    model carries no per-run state, so reusing one instance is safe.
    """
    from forgeharness.models.openai_compatible import OpenAICompatibleModel

    return lambda: OpenAICompatibleModel(config, client=client)


# Per-request output ceiling for live runs. The benchmark's own tasks answer in
# ~100-450 tokens, so this is generous for correctness while stopping the server
# from reserving its full context window on every one of hundreds of requests.
# Without it a long live run exhausts unified memory and can freeze the host.
LIVE_MAX_OUTPUT_TOKENS = 1024


def live_eval_config(
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int = LIVE_MAX_OUTPUT_TOKENS,
) -> OpenAICompatibleConfig:
    """Build the model config a live evaluation uses.

    Centralised so the memory-relevant defaults cannot drift between call sites:
    an unbounded ``max_tokens`` is what lets a long run exhaust the host, and
    thinking output is disabled because the benchmark grades the tool call, not
    the reasoning trace.
    """
    return OpenAICompatibleConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        enable_thinking=False,
    )


async def run_live_agent_evaluation(
    *,
    config: OpenAICompatibleConfig,
    manifest_path: Path,
    output_path: Path,
    project_root: Path,
    split: Split,
    profile: Profile,
    samples: int,
    strategies: tuple[Strategy, ...] = (Strategy.REACT, Strategy.PLAN_EXECUTE),
    limit: int | None = None,
    only_cases: tuple[str, ...] = (),
) -> AgentEvaluationReport:
    """Run a live profile, owning the shared HTTP client for its whole duration.

    A loopback model server must not be reached through a desktop proxy, so the
    client is created with delegation to the environment disabled; this matches
    the other local clients in the repository.
    """
    async with httpx.AsyncClient(
        timeout=config.timeout_seconds, trust_env=not _is_loopback_url(config.base_url)
    ) as client:
        return await run_agent_evaluation(
            manifest_path=manifest_path,
            output_path=output_path,
            project_root=project_root,
            split=split,
            strategies=strategies,
            profile=profile,
            model_factory=omlx_model_factory(config, client),
            model_name=config.model,
            # Pinned because the same model at a different temperature or output
            # ceiling is a different experiment.
            model_config=config.model_dump(mode="json"),
            samples=samples,
            limit=limit,
            only_cases=only_cases,
        )


def _is_loopback_url(base_url: str) -> bool:
    """True when the endpoint is a loopback host, where a proxy is never correct."""
    from urllib.parse import urlparse

    return urlparse(base_url).hostname in {"127.0.0.1", "localhost", "::1"}


async def _revision(project_root: Path) -> str:
    commit = await run_command(("git", "rev-parse", "HEAD"), workspace=project_root)
    return commit.output.strip() if commit.exit_code == 0 else "uncommitted"

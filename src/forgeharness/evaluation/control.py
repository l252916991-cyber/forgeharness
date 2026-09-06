"""Manifest-driven, keyless checks for Harness control behavior."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import perf_counter

from pydantic import Field, model_validator

from forgeharness.domain.models import (
    FinalAction,
    FrozenModel,
    ModelResult,
    ToolAction,
    ToolCall,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import HashChainedJSONLTrace, verify_trace
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.memory import SQLiteMemoryStore
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType, RiskBasedPolicy
from forgeharness.tools.process import run_command
from forgeharness.tools.registry import ToolRegistry


class ControlKind(StrEnum):
    """Implemented deterministic control scenarios."""

    TOOL_SUCCESS = "tool_success"
    UNKNOWN_TOOL_RECOVERY = "unknown_tool_recovery"
    TOOL_BUDGET_EXHAUSTION = "tool_budget_exhaustion"
    MODEL_FAILURE = "model_failure"
    APPROVAL_SUSPENSION = "approval_suspension"
    TRACE_TAMPER_DETECTION = "trace_tamper_detection"
    MEMORY_REVIEW_GATE = "memory_review_gate"


class ControlCase(FrozenModel):
    """One immutable expected outcome from the checked-in manifest."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: ControlKind
    expected: str = Field(min_length=1)


class ControlManifest(FrozenModel):
    """Versioned deterministic case list."""

    schema_version: int = Field(ge=1)
    cases: tuple[ControlCase, ...]

    @model_validator(mode="after")
    def unique_case_ids(self) -> ControlManifest:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("control case ids must be unique")
        return self


class ControlCaseResult(FrozenModel):
    """Observed outcome and compact reproducibility metrics."""

    id: str
    kind: ControlKind
    expected: str
    actual: str
    passed: bool
    elapsed_ms: float = Field(ge=0)
    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    trace_events: int = Field(default=0, ge=0)


class ControlReport(FrozenModel):
    """Machine-readable output suitable for CI and portfolio evidence."""

    schema_version: int
    generated_at: datetime
    revision: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    cases: tuple[ControlCaseResult, ...]


class _RequireApprovalPolicy:
    def evaluate(self, *, task_id: str, spec: object, call: ToolCall) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(
            type=PolicyDecisionType.REQUIRE_APPROVAL,
            reason="control case requires suspension",
        )


async def run_control_evaluation(
    *, manifest_path: Path, output_path: Path, project_root: Path
) -> ControlReport:
    """Execute every manifest case, write one JSON report, and return it."""
    manifest_bytes = await asyncio.to_thread(manifest_path.read_bytes)
    manifest = ControlManifest.model_validate_json(manifest_bytes)
    work_root = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="forgeharness-control-"))
    try:
        results = tuple([await _run_case(case, work_root / case.id) for case in manifest.cases])
    finally:
        await asyncio.to_thread(shutil.rmtree, work_root)
    report = ControlReport(
        schema_version=manifest.schema_version,
        generated_at=datetime.now(UTC),
        revision=await _revision(project_root),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        total=len(results),
        passed=sum(result.passed for result in results),
        cases=results,
    )
    await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        output_path.write_text,
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return report


async def _run_case(case: ControlCase, workspace: Path) -> ControlCaseResult:
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    started = perf_counter()
    steps = 0
    tool_calls = 0
    trace_events = 0
    if case.kind == ControlKind.TRACE_TAMPER_DETECTION:
        actual = _trace_tamper_case(workspace)
    elif case.kind == ControlKind.MEMORY_REVIEW_GATE:
        actual = _memory_review_case(workspace)
    else:
        actual, steps, tool_calls, trace_events = await _runtime_case(case, workspace)
    return ControlCaseResult(
        id=case.id,
        kind=case.kind,
        expected=case.expected,
        actual=actual,
        passed=actual == case.expected,
        elapsed_ms=round((perf_counter() - started) * 1000, 3),
        steps=steps,
        tool_calls=tool_calls,
        trace_events=trace_events,
    )


async def _runtime_case(case: ControlCase, workspace: Path) -> tuple[str, int, int, int]:
    registry = ToolRegistry()
    registry.register(EchoTool())
    trace = InMemoryTrace(case.id)
    responses: list[ModelResult]
    budget = RunBudget()
    policy: RiskBasedPolicy | _RequireApprovalPolicy = RiskBasedPolicy()
    if case.kind == ControlKind.TOOL_SUCCESS:
        responses = [_tool_response("echo"), _final_response()]
    elif case.kind == ControlKind.UNKNOWN_TOOL_RECOVERY:
        responses = [_tool_response("missing"), _final_response()]
    elif case.kind == ControlKind.TOOL_BUDGET_EXHAUSTION:
        responses = [_tool_response("echo")]
        budget = RunBudget(max_tool_calls=0)
    elif case.kind == ControlKind.MODEL_FAILURE:
        responses = []
    elif case.kind == ControlKind.APPROVAL_SUSPENSION:
        responses = [_tool_response("echo")]
        policy = _RequireApprovalPolicy()
    else:
        raise AssertionError(f"unhandled runtime control kind: {case.kind}")
    runtime = AgentRuntime(
        model=ScriptedModel(responses),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=policy,
        trace=trace,
        budget=budget,
    )
    result = await runtime.run(
        task_id=case.id,
        task="Execute deterministic control scenario",
        workspace=workspace,
    )
    return result.status.value, result.usage.steps, result.usage.tool_calls, len(trace.events)


def _tool_response(name: str) -> ModelResult:
    return ModelResult(
        action=ToolAction(call=ToolCall(id=f"{name}-1", name=name, arguments={"text": "control"})),
        model_name="scripted",
    )


def _final_response() -> ModelResult:
    return ModelResult(action=FinalAction(content="control completed"), model_name="scripted")


def _trace_tamper_case(workspace: Path) -> str:
    path = workspace / "trace.jsonl"
    trace = HashChainedJSONLTrace(path, "trace-tamper")
    trace.append("run.started", {"safe": True})
    line = json.loads(path.read_text(encoding="utf-8"))
    line["event"]["payload"] = {"safe": False}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return "detected" if not verify_trace(path).valid else "missed"


def _memory_review_case(workspace: Path) -> str:
    store = SQLiteMemoryStore(workspace / "memory.sqlite3")
    record = store.propose(
        task_id="memory-gate",
        content="Use the focused regression test.",
        evidence_ref="trace://memory-gate/1",
    )
    if store.search("regression"):
        return "leaked_candidate"
    store.review(record.id, approve=True)
    return "gated" if store.search("regression") else "missing_approved"


async def _revision(project_root: Path) -> str:
    commit = await run_command(("git", "rev-parse", "HEAD"), workspace=project_root)
    if commit.exit_code != 0:
        return "uncommitted"
    # Dirty content is frozen separately by reports/candidate-manifest.json. Keep
    # this field a standard revision so every report can share one release key.
    return commit.output.strip()

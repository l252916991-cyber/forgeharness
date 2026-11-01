"""Governed kernel, orchestrator contract and runtime bridges.

``RunKernel`` is the only sanctioned way for an application-provided
``Orchestrator`` to touch models, read-only tools, budgets, checkpoints and the
verifier (ADR 023). The bridges wrap host-supplied objects with the application's
evidence transform so nothing unredacted reaches the trace.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from pydantic import BaseModel

from forgeharness.context.assembly import ContextAssembler
from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    RunContext,
    RunResult,
    RunStatus,
    ToolCall,
    ToolEvidence,
    Usage,
)
from forgeharness.models.base import Model, ModelRequest
from forgeharness.models.caching import CachePlan, plan_cache
from forgeharness.models.prepared import (
    PreparedModelCall,
    decide_prepared_model,
    prepare_model_call,
)
from forgeharness.observability.trace import TraceRecorder
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.model_call import ModelCallKind, invoke_model_once
from forgeharness.runtime.tool_executor import ToolExecutionDenied, ToolExecutor
from forgeharness.runtime.verification import (
    VerificationRequest,
    VerificationResult,
    Verifier,
)
from forgeharness.sdk.policy import BoundPolicy
from forgeharness.sdk.privacy import EvidenceTransform
from forgeharness.sdk.spec import ScopeError
from forgeharness.state.checkpoint import CheckpointStore
from forgeharness.tools.base import (
    Tool,
    ToolContext,
    ToolOutput,
    ToolSpec,
)
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecisionType
from forgeharness.tools.registry import ToolRegistry


class OrchestrationError(RuntimeError):
    """An application-provided orchestrator used the kernel incorrectly."""


class KernelBudgetExceeded(OrchestrationError):
    """The run exhausted its step, token, tool or wall-clock budget."""


class KernelToolDenied(OrchestrationError):
    """The kernel's read-only tool surface or policy refused a call."""


class ModelBridge:
    def __init__(
        self, model: Model, transform: EvidenceTransform, guard: Callable[[], None]
    ) -> None:
        self.model = model
        self.transform = transform
        self.guard = guard

    def cache_plan(self, request: ModelRequest) -> CachePlan:
        planner = getattr(self.model, "cache_plan", None)
        return (
            CachePlan.model_validate(planner(request)) if callable(planner) else plan_cache(request)
        )

    @property
    def last_cache_read_tokens(self) -> int:
        value = getattr(self.model, "last_cache_read_tokens", 0)
        return value if type(value) is int and value >= 0 else 0

    async def decide(self, request: ModelRequest) -> ModelResult:
        # Inputs have already been transformed before model.request is traced.
        self.guard()
        result = await self.model.decide(request)
        return ModelResult.model_validate(self.transform.transform(result.model_dump(mode="json")))

    def prepare_request(self, request: ModelRequest) -> PreparedModelCall:
        self.guard()
        prepared = prepare_model_call(self.model, request)
        # Inspect decoded structures: a credential key inside a serialized JSON
        # string must not escape the host's key-based evidence transform.
        raw = {"request": prepared.request.model_dump(mode="json"), "payload": prepared.payload}
        if self.transform.transform(raw) != raw:
            raise ScopeError("prepared model input must not contain credentials")
        return prepared

    async def decide_prepared(self, prepared: PreparedModelCall) -> ModelResult:
        self.guard()
        result = await decide_prepared_model(self.model, prepared)
        return ModelResult.model_validate(self.transform.transform(result.model_dump(mode="json")))


class ToolBridge:
    def __init__(self, tool: Tool, transform: EvidenceTransform) -> None:
        self.tool = tool
        self.transform = transform

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec.model_validate(
            self.transform.transform(self.tool.spec.model_dump(mode="json"))
        )

    @property
    def input_model(self) -> type[BaseModel]:
        return self.tool.input_model

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        output = await self.tool.execute(arguments, context)
        return ToolOutput.model_validate(self.transform.transform(output.model_dump(mode="json")))


class TraceBridge:
    def __init__(self, trace: TraceRecorder, transform: EvidenceTransform) -> None:
        self.trace = trace
        self.transform = transform

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> Any:
        return self.trace.append(event_type, self.transform.transform(payload or {}))


class RunKernel:
    """Governed model, read-only tool, trace, budget and checkpoint surface.

    An application-provided orchestrator owns control flow; the kernel owns the
    calls. Model requests use the same prepared-input budget, evidence transform
    and hash-chained trace as ReAct. Tool calls are deliberately limited to
    ``RiskLevel.READ`` because this kernel does not yet own approval or
    invocation-journal semantics for side effects. A custom run is one-shot:
    approval/resume stay on the ReAct path until a later ADR.

    Hosts do not construct this directly; ``Harness`` builds it and passes it to
    ``Orchestrator.run``.
    """

    def __init__(
        self,
        *,
        context: RunContext,
        task_id: str,
        workspace: Path | None,
        model: ModelBridge,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        policy: BoundPolicy,
        trace: TraceBridge,
        budget: RunBudget,
        verifier: Verifier,
        checkpoint_store: CheckpointStore | None,
    ) -> None:
        self._context = context
        self._task_id = task_id
        self._workspace = workspace
        self._model = model
        self._registry = registry
        self._dispatcher = dispatcher
        self._policy = policy
        self._trace = trace
        self._budget = budget
        self._verifier = verifier
        self._checkpoints = checkpoint_store
        self._assembler = ContextAssembler(max_tokens=budget.max_context_tokens)
        self.usage = Usage()
        self.messages: list[Message] = []
        self.evidence: list[ToolEvidence] = []
        self._started = False
        self._finished = False
        self._checkpoint_revision = 0
        self._deadline: float | None = None
        # One in-process budget writer for both models and tools. Durable
        # fan-out/recovery remains outside this one-shot kernel's contract.
        self._invocation_lock = asyncio.Lock()
        self._tools = ToolExecutor(
            registry=registry,
            dispatcher=dispatcher,
            policy=policy,
            trace=trace,
            budget=budget,
            context=lambda: self._context,
        )

    @property
    def context(self) -> RunContext:
        return self._context

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def workspace(self) -> Path | None:
        return self._workspace

    @property
    def budget(self) -> RunBudget:
        return self._budget

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def remaining(self) -> dict[str, int]:
        return {
            "steps": max(0, self._budget.max_steps - self.usage.steps),
            "tool_calls": max(0, self._budget.max_tool_calls - self.usage.tool_calls),
            "input_tokens": max(0, self._budget.max_input_tokens - self.usage.input_tokens),
            "output_tokens": max(0, self._budget.max_output_tokens - self.usage.output_tokens),
        }

    def start(self, *, task: str, instructions: tuple[Message, ...] = ()) -> None:
        """Initialize the transcript and save the first RUNNING checkpoint."""
        if self._started:
            raise OrchestrationError("kernel already started")
        self._started = True
        self.messages = [*instructions, Message(role=MessageRole.USER, content=task)]
        if self._budget.max_wall_seconds is not None:
            self._deadline = monotonic() + self._budget.max_wall_seconds
        self._trace.append(
            "run.started",
            {
                "workspace": str(self._workspace),
                "orchestrator": "custom",
            },
        )
        self._save(RunStatus.RUNNING)

    def checkpoint(self) -> RunResult:
        """Persist the current transcript/usage as a RUNNING checkpoint."""
        self._ensure_active()
        return self._save(RunStatus.RUNNING)

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Record an orchestrator-owned trace event through the evidence transform."""
        if not self._started:
            raise OrchestrationError("kernel not started")
        self._trace.append(event_type, payload or {})

    async def call_model(
        self,
        *,
        messages: Sequence[Message] | None = None,
        phase: str = "orchestrator",
        include_tools: bool = True,
    ) -> ModelResult:
        """Call with an optional tool catalog, under the shared budget.

        ``include_tools=False`` omits tool schemas for final-text phases. It
        changes only this request's model-visible catalog, never tool authority.
        """
        if not isinstance(include_tools, bool):
            raise OrchestrationError("include_tools must be a boolean")
        async with self._invocation_lock:
            return await self._call_model(
                messages=messages, phase=phase, include_tools=include_tools
            )

    async def _call_model(
        self,
        *,
        messages: Sequence[Message] | None = None,
        phase: str = "orchestrator",
        include_tools: bool = True,
    ) -> ModelResult:
        """Call the model through the same shared governed path as ReAct.

        Accounting matches ReAct exactly: a success charges one step plus the
        provider tokens; a recoverable protocol violation charges one step plus
        the rejected request's tokens before raising, so a custom orchestrator
        that retries inside its budget pays for the inference that really
        happened. The kernel does not auto-retry violations; the orchestrator
        decides.
        """
        self._ensure_active()
        self._check_deadline()
        if self.usage.steps >= self._budget.max_steps:
            raise KernelBudgetExceeded("step budget exhausted")
        outcome = await invoke_model_once(
            model=self._model,
            request=ModelRequest(
                task_id=self._task_id,
                messages=tuple(messages) if messages is not None else tuple(self.messages),
                tools=self._registry.specs() if include_tools else (),
            ),
            budget=self._budget,
            assembler=self._assembler,
            trace=self._trace,
            usage=self.usage,
            phase=phase,
        )
        self.usage = outcome.usage
        if outcome.kind is ModelCallKind.CONTEXT_OVERFLOW:
            raise KernelBudgetExceeded("context budget exceeded")
        if outcome.kind is ModelCallKind.PREPARATION_FAILED:
            raise OrchestrationError("model request preparation failed")
        if outcome.kind is ModelCallKind.MODEL_FAILED:
            raise OrchestrationError(f"model call failed: {outcome.error_type}")
        if outcome.kind is ModelCallKind.PROTOCOL_VIOLATION:
            code = getattr(outcome.violation, "code", "model_protocol_error")
            raise OrchestrationError(f"model protocol violation: {code}")
        if self._tokens_exhausted():
            raise KernelBudgetExceeded("token budget exhausted")
        if outcome.result is None:
            raise OrchestrationError("model call returned no result")
        return outcome.result

    async def call_tool(self, call: ToolCall) -> ToolOutput:
        """Execute through the shared tool executor with serialized admission."""
        async with self._invocation_lock:
            return await self._call_tool(call)

    async def _call_tool(self, call: ToolCall) -> ToolOutput:
        """Call a read-only tool after current policy approval.

        Write/process/network tools and approval-required calls are rejected:
        this kernel has no approval delegation or invocation journal, so it must
        not pretend to offer the ReAct side-effect contract.
        """
        self._ensure_active()
        self._check_deadline()
        if self.usage.tool_calls >= self._budget.max_tool_calls:
            raise KernelBudgetExceeded("tool-call budget exhausted")
        tool = self._registry.get(call.name)
        if tool is None:
            raise OrchestrationError(f"unknown tool: {call.name}")
        try:
            decision = self._tools.authorize(call, task_id=self._task_id, read_only=True)
        except ToolExecutionDenied as exc:
            raise KernelToolDenied(str(exc)) from exc
        if decision.type is not PolicyDecisionType.ALLOW:
            raise KernelToolDenied(f"tool not allowed: {decision.reason}")
        self._trace.append(
            "tool.started",
            {"call_id": call.id, "tool": call.name},
        )
        dispatch = await self._tools.dispatch(
            call,
            self._workspace,
            self.usage,
            task_id=self._task_id,
        )
        self.usage = ToolExecutor.account_usage(self.usage, dispatch.output.usage)
        self.evidence.append(
            ToolEvidence(
                name=call.name,
                ok=dispatch.output.ok,
                content=dispatch.output.content,
                metadata=dispatch.output.metadata,
                logical_call_id=call.id,
                receipt=dispatch.output.receipt,
            )
        )
        self._trace.append(
            "tool.completed",
            {
                "call_id": call.id,
                "tool": call.name,
                "ok": dispatch.output.ok,
                "elapsed_ms": dispatch.elapsed_ms,
                "content_chars": len(dispatch.output.content),
                "budget": self.remaining,
            },
        )
        if (
            self.usage.steps > self._budget.max_steps
            or self.usage.tool_calls > self._budget.max_tool_calls
            or self.usage.input_tokens > self._budget.max_input_tokens
            or self.usage.output_tokens > self._budget.max_output_tokens
        ):
            raise KernelBudgetExceeded("nested tool budget exhausted")
        return dispatch.output

    async def verify(self, final: FinalAction) -> VerificationResult:
        """Run the application verifier with ordered tool evidence."""
        self._ensure_active()
        try:
            result = await self._verifier.verify(
                VerificationRequest(
                    task_id=self._task_id,
                    final=final,
                    workspace=self._workspace,
                    tool_results=tuple(self.evidence),
                    run_context=self._context,
                )
            )
        except Exception as exc:
            self._trace.append(
                "verification.failed",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise OrchestrationError(f"verification failed: {type(exc).__name__}") from exc
        self._trace.append(
            "verification.passed" if result.passed else "verification.rejected",
            {"reason": result.reason, "evidence": list(result.evidence)},
        )
        return result

    async def finish(
        self,
        *,
        final: FinalAction | None = None,
        status: RunStatus = RunStatus.SUCCEEDED,
        error: str | None = None,
    ) -> RunResult:
        """Verify (on success) and persist the terminal checkpoint."""
        self._ensure_active()
        if status in {
            RunStatus.CREATED,
            RunStatus.RUNNING,
            RunStatus.AWAITING_APPROVAL,
        }:
            raise OrchestrationError(f"invalid terminal status: {status.value}")
        final_output: str | None = None
        if status is RunStatus.SUCCEEDED:
            if final is None:
                status = RunStatus.FAILED
                error = "orchestrator reported success without a final action"
            else:
                verdict = await self.verify(final)
                if verdict.passed:
                    final_output = final.content
                else:
                    status = RunStatus.FAILED
                    error = f"verification rejected: {verdict.reason}"
        if status is RunStatus.FAILED and error is None:
            error = "orchestrator failed"
        self._trace.append(
            "run.finished",
            {"status": status.value, "error": error},
        )
        result = self._save(status, final_output=final_output, error=error)
        self._finished = True
        return result

    def _save(
        self,
        status: RunStatus,
        *,
        final_output: str | None = None,
        error: str | None = None,
    ) -> RunResult:
        result = RunResult(
            task_id=self._task_id,
            status=status,
            messages=tuple(self.messages),
            usage=self.usage,
            checkpoint_revision=self._checkpoint_revision,
            final_output=final_output,
            error=error,
            run_context=self._context,
            tool_evidence=tuple(self.evidence),
        )
        if self._checkpoints is None:
            self._checkpoint_revision += 1
            return result
        saved = self._checkpoints.save(result)
        self._checkpoint_revision = saved.checkpoint_revision
        self._trace.append(
            "checkpoint.saved",
            {"revision": saved.checkpoint_revision, "status": status.value},
        )
        return saved

    def _ensure_started(self) -> None:
        if not self._started:
            raise OrchestrationError("kernel not started")

    def _ensure_active(self) -> None:
        self._ensure_started()
        if self._finished:
            raise OrchestrationError("kernel already finished")

    def _check_deadline(self) -> None:
        if self._deadline is not None and monotonic() >= self._deadline:
            raise KernelBudgetExceeded("deadline exceeded")

    def _tokens_exhausted(self) -> bool:
        return (
            self.usage.input_tokens >= self._budget.max_input_tokens
            or self.usage.output_tokens >= self._budget.max_output_tokens
        )


class Orchestrator(Protocol):
    """Application-owned control flow over a governed kernel."""

    async def run(self, *, kernel: RunKernel, task: str) -> RunResult: ...

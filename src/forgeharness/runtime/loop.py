"""Minimal model/tool loop with Harness-owned policy, budget, and trace."""

from __future__ import annotations

from pathlib import Path

from forgeharness.context.compression import ObservationCompressor
from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    PendingApproval,
    RunResult,
    RunStatus,
    ToolAction,
    Usage,
)
from forgeharness.models.base import Model, ModelRequest
from forgeharness.observability.trace import TraceRecorder
from forgeharness.runtime.budget import RunBudget
from forgeharness.state.approval import ApprovalGrant, ApprovalLedger
from forgeharness.state.checkpoint import CheckpointConflict, CheckpointStore
from forgeharness.tools.base import ExecutionAllowance, ToolContext
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecisionType, ToolPolicy
from forgeharness.tools.registry import ToolRegistry


class AgentRuntime:
    """Execute validated model decisions under deterministic Harness controls."""

    def __init__(
        self,
        *,
        model: Model,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        policy: ToolPolicy,
        trace: TraceRecorder,
        budget: RunBudget | None = None,
        approval_ledger: ApprovalLedger | None = None,
        checkpoint_store: CheckpointStore | None = None,
        observation_compressor: ObservationCompressor | None = None,
    ) -> None:
        self._model = model
        self._registry = registry
        self._dispatcher = dispatcher
        self._policy = policy
        self._trace = trace
        self._budget = budget or RunBudget()
        self._approval_ledger = approval_ledger
        self._checkpoint_store = checkpoint_store
        self._observation_compressor = observation_compressor or ObservationCompressor()

    async def run(
        self,
        *,
        task_id: str,
        task: str,
        workspace: Path,
        instructions: tuple[Message, ...] = (),
        initial_usage: Usage | None = None,
    ) -> RunResult:
        """Run until a final answer, approval suspension, failure, or budget exhaustion."""
        if any(message.role != MessageRole.SYSTEM for message in instructions):
            raise ValueError("runtime instructions must be system messages")
        messages = [*instructions, Message(role=MessageRole.USER, content=task)]
        usage = initial_usage or Usage()
        self._trace.append("run.started", {"workspace": str(workspace)})
        initial = self._checkpoint(
            RunResult(
                task_id=task_id,
                status=RunStatus.RUNNING,
                messages=tuple(messages),
                usage=usage,
            )
        )
        return await self._drive(
            task_id=task_id,
            messages=messages,
            usage=usage,
            workspace=workspace,
            checkpoint_revision=initial.checkpoint_revision,
        )

    async def resume_approved(
        self, *, previous: RunResult, grant: ApprovalGrant, workspace: Path
    ) -> RunResult:
        """Consume an exact approval, execute the suspended call, and continue the run."""
        if previous.status != RunStatus.AWAITING_APPROVAL or previous.pending_approval is None:
            raise ValueError("only an awaiting-approval run can be resumed")
        if self._approval_ledger is None:
            raise RuntimeError("runtime has no approval ledger")
        self._assert_current(previous)
        call = previous.pending_approval.call
        tool = self._registry.get(call.name)
        if tool is None:
            raise ValueError(f"approved tool is no longer registered: {call.name}")
        current_decision = self._policy.evaluate(
            task_id=previous.task_id, spec=tool.spec, call=call
        )
        if current_decision.type == PolicyDecisionType.DENY:
            raise ValueError(f"approved tool is now denied: {current_decision.reason}")
        self._approval_ledger.consume(grant, task_id=previous.task_id, call=call)
        self._trace.append("approval.consumed", {"tool": call.name, "granted_by": grant.granted_by})
        messages = list(previous.messages)
        dispatch = await self._dispatcher.dispatch(
            call, self._tool_context(previous.task_id, workspace, previous.usage)
        )
        usage = self._account_tool_usage(previous.usage, dispatch.output.usage)
        observation = self._observation(dispatch.output.content)
        messages.append(
            Message(
                role=MessageRole.TOOL,
                content=observation,
                tool_call_id=call.id,
                tool_name=call.name,
            )
        )
        self._trace.append(
            "tool.completed",
            {
                "tool": call.name,
                "ok": dispatch.output.ok,
                "elapsed_ms": dispatch.elapsed_ms,
                "observation": observation,
                "metadata": dispatch.output.metadata,
            },
        )
        running = self._checkpoint(
            RunResult(
                task_id=previous.task_id,
                status=RunStatus.RUNNING,
                messages=tuple(messages),
                usage=usage,
                checkpoint_revision=previous.checkpoint_revision,
            )
        )
        return await self._drive(
            task_id=previous.task_id,
            messages=messages,
            usage=usage,
            workspace=workspace,
            checkpoint_revision=running.checkpoint_revision,
        )

    async def _drive(
        self,
        *,
        task_id: str,
        messages: list[Message],
        usage: Usage,
        workspace: Path,
        checkpoint_revision: int,
    ) -> RunResult:
        """Continue a new or resumed run from validated in-memory state."""

        while usage.steps < self._budget.max_steps:
            if self._tokens_exhausted(usage):
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="token budget exhausted",
                )
            try:
                model_result = await self._model.decide(
                    ModelRequest(
                        task_id=task_id,
                        messages=tuple(messages),
                        tools=self._registry.specs(),
                    )
                )
            except Exception as exc:
                self._trace.append(
                    "model.failed", {"error_type": type(exc).__name__, "error": str(exc)}
                )
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=f"model failed: {type(exc).__name__}: {exc}",
                )

            usage = Usage(
                steps=usage.steps + 1,
                tool_calls=usage.tool_calls,
                input_tokens=usage.input_tokens + model_result.usage.input_tokens,
                output_tokens=usage.output_tokens + model_result.usage.output_tokens,
            )
            self._trace.append(
                "model.action",
                {
                    "kind": model_result.action.kind,
                    "model_name": model_result.model_name,
                    "input_tokens": model_result.usage.input_tokens,
                    "output_tokens": model_result.usage.output_tokens,
                },
            )

            if self._tokens_exhausted(usage):
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="token budget exhausted",
                )

            action = model_result.action
            if isinstance(action, FinalAction):
                messages.append(Message(role=MessageRole.ASSISTANT, content=action.content))
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.SUCCEEDED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    final_output=action.content,
                )

            if not isinstance(action, ToolAction):
                raise AssertionError(f"unhandled action type: {type(action).__name__}")

            if usage.tool_calls >= self._budget.max_tool_calls:
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="tool-call budget exhausted",
                )

            messages.append(Message(role=MessageRole.ASSISTANT, tool_calls=(action.call,)))
            tool = self._registry.get(action.call.name)
            if tool is None:
                messages.append(self._tool_message(action, f"unknown tool: {action.call.name}"))
                self._trace.append("tool.unknown", {"tool": action.call.name})
                usage = usage.model_copy(update={"tool_calls": usage.tool_calls + 1})
                checkpoint_revision = self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            decision = self._policy.evaluate(task_id=task_id, spec=tool.spec, call=action.call)
            self._trace.append(
                "policy.decided",
                {
                    "tool": action.call.name,
                    "decision": decision.type.value,
                    "reason": decision.reason,
                },
            )
            if decision.type == PolicyDecisionType.REQUIRE_APPROVAL:
                self._trace.append("run.awaiting_approval", {"tool": action.call.name})
                return self._checkpoint(
                    RunResult(
                        task_id=task_id,
                        status=RunStatus.AWAITING_APPROVAL,
                        messages=tuple(messages),
                        usage=usage,
                        checkpoint_revision=checkpoint_revision,
                        pending_approval=PendingApproval(call=action.call, reason=decision.reason),
                    )
                )
            if decision.type == PolicyDecisionType.DENY:
                messages.append(
                    self._tool_message(action, f"policy denied tool: {decision.reason}")
                )
                usage = usage.model_copy(update={"tool_calls": usage.tool_calls + 1})
                checkpoint_revision = self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            dispatch = await self._dispatcher.dispatch(
                action.call, self._tool_context(task_id, workspace, usage)
            )
            usage = self._account_tool_usage(usage, dispatch.output.usage)
            observation = self._observation(dispatch.output.content)
            messages.append(self._tool_message(action, observation))
            self._trace.append(
                "tool.completed",
                {
                    "tool": action.call.name,
                    "ok": dispatch.output.ok,
                    "elapsed_ms": dispatch.elapsed_ms,
                    "observation": observation,
                    "metadata": dispatch.output.metadata,
                },
            )
            checkpoint_revision = self._save_running(task_id, messages, usage, checkpoint_revision)

        return self._finish(
            task_id=task_id,
            status=RunStatus.EXHAUSTED,
            messages=messages,
            usage=usage,
            checkpoint_revision=checkpoint_revision,
            error="step budget exhausted",
        )

    def _tokens_exhausted(self, usage: Usage) -> bool:
        return (
            usage.input_tokens >= self._budget.max_input_tokens
            or usage.output_tokens >= self._budget.max_output_tokens
        )

    def _tool_context(self, task_id: str, workspace: Path, usage: Usage) -> ToolContext:
        return ToolContext(
            task_id=task_id,
            workspace=workspace,
            allowance=ExecutionAllowance(
                remaining_steps=max(0, self._budget.max_steps - usage.steps),
                remaining_tool_calls=max(0, self._budget.max_tool_calls - usage.tool_calls - 1),
                remaining_input_tokens=max(0, self._budget.max_input_tokens - usage.input_tokens),
                remaining_output_tokens=max(
                    0, self._budget.max_output_tokens - usage.output_tokens
                ),
            ),
        )

    def _observation(self, content: str) -> str:
        return self._observation_compressor.compress(content)

    @staticmethod
    def _tool_message(action: ToolAction, content: str) -> Message:
        return Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=action.call.id,
            tool_name=action.call.name,
        )

    def _finish(
        self,
        *,
        task_id: str,
        status: RunStatus,
        messages: list[Message],
        usage: Usage,
        checkpoint_revision: int,
        final_output: str | None = None,
        error: str | None = None,
    ) -> RunResult:
        self._trace.append("run.finished", {"status": status.value, "error": error})
        return self._checkpoint(
            RunResult(
                task_id=task_id,
                status=status,
                messages=tuple(messages),
                usage=usage,
                checkpoint_revision=checkpoint_revision,
                final_output=final_output,
                error=error,
            )
        )

    def _save_running(
        self, task_id: str, messages: list[Message], usage: Usage, revision: int
    ) -> int:
        saved = self._checkpoint(
            RunResult(
                task_id=task_id,
                status=RunStatus.RUNNING,
                messages=tuple(messages),
                usage=usage,
                checkpoint_revision=revision,
            )
        )
        return saved.checkpoint_revision

    @staticmethod
    def _account_tool_usage(usage: Usage, child: Usage | None) -> Usage:
        nested = child or Usage()
        return Usage(
            steps=usage.steps + nested.steps,
            tool_calls=usage.tool_calls + 1 + nested.tool_calls,
            input_tokens=usage.input_tokens + nested.input_tokens,
            output_tokens=usage.output_tokens + nested.output_tokens,
        )

    def _checkpoint(self, result: RunResult) -> RunResult:
        if self._checkpoint_store is None:
            return result
        saved = self._checkpoint_store.save(result)
        self._trace.append(
            "checkpoint.saved", {"revision": saved.checkpoint_revision, "status": saved.status}
        )
        return saved

    def _assert_current(self, result: RunResult) -> None:
        if self._checkpoint_store is None:
            return
        current = self._checkpoint_store.load(result.task_id)
        if current is None or current.checkpoint_revision != result.checkpoint_revision:
            raise CheckpointConflict(f"run {result.task_id} is not the current checkpoint")

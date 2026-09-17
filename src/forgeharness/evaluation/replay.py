"""Minimal deterministic replay of a Bad Case.

`RecordedModel` replays what the model produced *at its boundary* — a validated
action, or the violation that stopped the run — instead of pretending to be the
model. Holding the model's choices constant is what lets a replay prove a
*harness* fix: if the runtime now handles the same recorded violation better, the
improvement cannot be the model's doing.

The scope is deliberately minimal. This answers one question:

    can this real failure be reproduced without depending on model randomness?

It does not do trace editing, branching, replay-from-checkpoint, or diffing, and
it cannot show that a prompt or planner change helped — that needs a live
re-evaluation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from forgeharness.domain.models import ModelResult, RunResult
from forgeharness.evaluation.bad_case import BadCase, ReplayStep
from forgeharness.models.base import ModelProtocolError, ModelRequest
from forgeharness.observability.steps import RunProjection, project_events
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.plan_execute import PlanExecuteRuntime
from forgeharness.tools.base import Tool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.registry import ToolRegistry


class ReplayExhausted(RuntimeError):
    """The replay script ran out of frozen outcomes before the run finished."""


class RecordedModel:
    """Replay a frozen sequence of model outcomes, violations included.

    A frozen violation is re-raised as a real `ModelProtocolError` carrying its
    recorded code, count and usage, so the runtime takes the same decision path it
    took live — including any recovery the harness has since grown.
    """

    def __init__(self, script: tuple[ReplayStep, ...]) -> None:
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    async def decide(self, request: ModelRequest) -> ModelResult:
        """Return the next frozen outcome, or re-raise its recorded violation."""
        self.requests.append(request)
        if not self._script:
            raise ReplayExhausted("replay script has no outcome remaining")
        step = self._script.pop(0)
        if step.error is not None:
            raise ModelProtocolError(
                step.error.message or step.error.error_type,
                code=step.error.code or "model_protocol_error",
                received_tool_calls=step.error.received_tool_calls,
                usage=step.error.usage,
                recoverable=step.error.recoverable,
            )
        if step.result is None:
            raise ReplayExhausted("replay step carries no outcome")
        return step.result

    @property
    def remaining(self) -> int:
        """Frozen outcomes not yet consumed."""
        return len(self._script)


class RegistryBuilder(Protocol):
    """Builds the tool registry a replay runs against."""

    def __call__(self, case_id: str, workspace: Path) -> ToolRegistry:
        """Return a registry whose tools are confined to `workspace`."""
        ...


class _AllowAllPolicy:
    """A replay measures harness behaviour, so policy must not suspend the run."""

    def evaluate(self, *, task_id: str, spec: object, call: object) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="bad-case replay")


async def replay_bad_case(
    bad_case: BadCase,
    *,
    workspace: Path,
    task: str,
    build_registry: RegistryBuilder,
) -> tuple[RunResult, RunProjection]:
    """Run a Bad Case against its frozen model behaviour using today's harness.

    The registry is rebuilt from the current benchmark definition rather than
    frozen, precisely so the replay exercises the *current* runtime and tools
    against the *recorded* model choices — which is what makes it a regression
    test for a harness fix.
    """
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    trace = InMemoryTrace(bad_case.case_id)
    registry = build_registry(bad_case.case_id, workspace)
    executor = AgentRuntime(
        model=RecordedModel(bad_case.executor_script),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAllPolicy(),
        trace=trace,
        budget=RunBudget(),
    )
    runtime: AgentRuntime | PlanExecuteRuntime
    if bad_case.planner_script is None:
        runtime = executor
    else:
        runtime = PlanExecuteRuntime(
            planner=RecordedModel(bad_case.planner_script), executor=executor, trace=trace
        )
    result = await runtime.run(task_id=bad_case.case_id, task=task, workspace=workspace)
    return result, project_events(trace.events)


def registry_from_tools(tools: tuple[Tool, ...]) -> RegistryBuilder:
    """Build a registry builder from an explicit tool set."""

    def build(case_id: str, workspace: Path) -> ToolRegistry:
        del case_id, workspace
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        return registry

    return build

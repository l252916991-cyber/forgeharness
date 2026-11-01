"""Scoped sub-agent delegation whose usage is charged to the parent run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import FrozenModel, Message, MessageRole
from forgeharness.models.base import Model
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.tools.base import (
    ExecutionAllowance,
    RiskLevel,
    Tool,
    ToolContext,
    ToolOutput,
    ToolSpec,
)
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import ToolPolicy
from forgeharness.tools.registry import ToolRegistry


class SubAgentSpec(FrozenModel):
    """Operator-owned identity, instructions, capabilities, and hard limits."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=4_000)
    #: Compiled skill text this sub-agent is assigned. Kept separate from
    #: ``instructions`` so an installed package can be attached without rewriting the
    #: operator's own brief, and bounded because it lands in every child request.
    skill_instructions: str = Field(default="", max_length=20_000)
    allowed_risks: tuple[RiskLevel, ...] = (RiskLevel.READ,)
    max_steps: int = Field(default=6, ge=1, le=50)
    max_tool_calls: int = Field(default=8, ge=0, le=100)
    max_input_tokens: int = Field(default=30_000, ge=0)
    max_output_tokens: int = Field(default=6_000, ge=0)


class DelegateInput(BaseModel):
    """A bounded subtask supplied by the parent model."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=4_000)


class SubAgentTool:
    """Expose one preconfigured sub-agent as a native, policy-visible tool."""

    input_model = DelegateInput

    def _composed_instructions(self) -> str:
        """Return the operator brief followed by any assigned skill text.

        The brief comes first on purpose: a skill is reusable advice, and the operator's
        scope and limits are what make it applicable here. Putting the package first
        would let a downloaded skill outrank the delegation boundary.
        """
        skill = self._subagent_spec.skill_instructions.strip()
        if not skill:
            return self._subagent_spec.instructions
        return f"{self._subagent_spec.instructions}\n\n{skill}"

    def __init__(
        self,
        *,
        spec: SubAgentSpec,
        model: Model,
        tools: tuple[Tool, ...],
        policy: ToolPolicy,
        timeout_seconds: float = 60.0,
    ) -> None:
        forbidden = [tool.spec.name for tool in tools if tool.spec.risk not in spec.allowed_risks]
        if forbidden:
            names = ", ".join(forbidden)
            raise ValueError(
                f"sub-agent {spec.name} received tools outside its risk scope: {names}"
            )
        self._subagent_spec = spec
        self._model = model
        self._tools = tools
        self._policy = policy
        self._timeout_seconds = timeout_seconds
        self.spec = ToolSpec(
            name=f"delegate_{spec.name}",
            description=spec.description,
            input_schema=DelegateInput.model_json_schema(),
            risk=max(spec.allowed_risks, key=_risk_rank, default=RiskLevel.READ),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Run the child with a reduced registry and return its trace and charged usage."""
        values = DelegateInput.model_validate(arguments)
        allowance = context.allowance
        if allowance is not None and allowance.remaining_steps == 0:
            return ToolOutput(
                ok=False,
                content="sub-agent not started: parent has no remaining model steps",
            )
        registry = ToolRegistry()
        for tool in self._tools:
            registry.register(tool)
        child_id = f"{context.task_id}.{self._subagent_spec.name}"
        trace = InMemoryTrace(child_id)
        runtime = AgentRuntime(
            model=self._model,
            registry=registry,
            dispatcher=ToolDispatcher(registry, timeout_seconds=self._timeout_seconds),
            policy=self._policy,
            trace=trace,
            budget=self._child_budget(allowance),
        )
        result = await runtime.run(
            task_id=child_id,
            task=values.task,
            workspace=context.require_workspace(),
            instructions=(Message(role=MessageRole.SYSTEM, content=self._composed_instructions()),),
        )
        content = result.final_output or result.error or f"sub-agent ended: {result.status.value}"
        return ToolOutput(
            ok=result.status.value == "succeeded",
            content=content,
            metadata={
                "child_task_id": child_id,
                "status": result.status.value,
                "trace": [event.model_dump(mode="json") for event in trace.events],
            },
            usage=result.usage,
        )

    def _child_budget(self, allowance: ExecutionAllowance | None) -> RunBudget:
        if allowance is None:
            return RunBudget(
                max_steps=self._subagent_spec.max_steps,
                max_tool_calls=self._subagent_spec.max_tool_calls,
                max_input_tokens=self._subagent_spec.max_input_tokens,
                max_output_tokens=self._subagent_spec.max_output_tokens,
            )
        return RunBudget(
            max_steps=min(self._subagent_spec.max_steps, allowance.remaining_steps),
            max_tool_calls=min(self._subagent_spec.max_tool_calls, allowance.remaining_tool_calls),
            max_input_tokens=min(
                self._subagent_spec.max_input_tokens, allowance.remaining_input_tokens
            ),
            max_output_tokens=min(
                self._subagent_spec.max_output_tokens, allowance.remaining_output_tokens
            ),
        )


def _risk_rank(risk: RiskLevel) -> int:
    return {
        RiskLevel.READ: 0,
        RiskLevel.WRITE: 1,
        RiskLevel.PROCESS: 2,
        RiskLevel.NETWORK: 3,
    }[risk]

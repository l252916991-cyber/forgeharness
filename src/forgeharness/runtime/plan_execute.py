"""A distinct planning phase followed by the controlled tool loop."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field, ValidationError

from forgeharness.domain.models import (
    FinalAction,
    FrozenModel,
    Message,
    MessageRole,
    ModelResult,
    RunResult,
    RunStatus,
    ToolAction,
    Usage,
)
from forgeharness.models.base import Model, ModelRequest
from forgeharness.observability.trace import TraceRecorder
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime


class ExecutionPlan(FrozenModel):
    """Structured steps generated before tool execution begins."""

    steps: tuple[str, ...] = Field(min_length=1, max_length=20)


class PlanProtocolViolation(ValueError):
    """A planner response that breaks the plan protocol.

    ``code`` names the specific defect so the runtime can decide whether a
    correction attempt is warranted, and so the trace records *what* was wrong
    rather than a prose message. The plans themselves are validated exactly as
    strictly as before: this class changes diagnostics, not tolerance.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_plan(content: str) -> ExecutionPlan:
    """Extract the plan JSON from a planner response, tolerating formatting noise.

    Models decorate JSON in ways that have nothing to do with planning ability:
    a ```json fence, a stray trailing backtick, or a sentence before the object.
    Rejecting those would score the model on markdown rather than on
    orchestration and would bias any runtime comparison. So the first JSON object
    in the response is extracted and *then* strictly validated: a plan that is
    empty, too long, not an object, or absent is still rejected exactly as before.

    The tolerance is only for decoration *around* a JSON object. A response with
    no object, invalid JSON syntax, or a shape that misses the schema is a
    violation — never guessed at or repaired.
    """
    start = content.find("{")
    if start == -1:
        raise PlanProtocolViolation(
            "planner response contains no JSON object", code="missing_plan_json"
        )
    try:
        # `raw_decode` stops at the end of the first JSON document, so anything
        # before or after it (fences, backticks, prose) is ignored.
        decoded, _ = json.JSONDecoder().raw_decode(content[start:])
    except json.JSONDecodeError as exc:
        raise PlanProtocolViolation(
            f"planner response is not valid JSON: {exc}", code="invalid_plan_json"
        ) from exc
    try:
        return ExecutionPlan.model_validate(decoded)
    except ValidationError as exc:
        raise PlanProtocolViolation(
            f"planner plan does not match the required shape: {exc}",
            code="plan_schema_violation",
        ) from exc


def _plan_correction_message(violation: PlanProtocolViolation) -> str:
    """Turn a planner violation into an actionable instruction.

    Deliberately does not forward the exception text: a decode traceback is not
    something a model can act on, and recycling raw exception strings is the exact
    defect this project is fixing elsewhere. It states the requirement instead.
    """
    detail = {
        "missing_plan_json": "no JSON object was found in the response",
        "invalid_plan_json": "the response was not valid JSON",
        "plan_schema_violation": (
            "the JSON did not match the required shape: a non-empty, at most 20-item "
            "array of step strings under the key `steps`"
        ),
    }.get(violation.code, "the response did not satisfy the plan protocol")
    return (
        "Harness protocol error: the previous planning response was rejected because "
        f"{detail}. No plan was created and nothing was executed. Reply with only raw "
        'JSON matching {"steps":["step"]} and no other text.'
    )


def _planner_response_payload(response: ModelResult) -> dict[str, object]:
    """Serialize a planner response so a rejected attempt can be replayed.

    A tool call is recorded as such; a text response keeps its content verbatim,
    because that content is what the parser rejected and what a replay must
    reproduce.
    """
    action = response.action
    payload: dict[str, object] = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "model_name": response.model_name,
    }
    if isinstance(action, ToolAction):
        payload["kind"] = "tool"
        payload["tool"] = action.call.name
        payload["arguments"] = dict(action.call.arguments)
    else:
        payload["kind"] = "final"
        payload["content"] = action.content
    return payload


class PlanExecuteRuntime:
    """Plan once under protocol control, then run the executor loop."""

    def __init__(
        self,
        *,
        planner: Model,
        executor: AgentRuntime,
        trace: TraceRecorder,
        max_planner_retries: int | None = None,
    ) -> None:
        """Bound planner protocol retries; the default allows a single correction.

        Kept as its own budget rather than sharing the executor's, because the two
        violations live in different lifecycles: a malformed plan and a multi-call
        step are both protocol errors but are diagnosed and bounded separately.
        """
        if max_planner_retries is None:
            # Share the default with the budget contract so the two cannot drift.
            max_planner_retries = RunBudget().max_planner_retries
        if max_planner_retries < 0:
            raise ValueError("max_planner_retries must not be negative")
        self._planner = planner
        self._executor = executor
        self._trace = trace
        self._max_planner_retries = max_planner_retries
        self._max_planner_attempts = max_planner_retries + 1

    async def run(self, *, task_id: str, task: str, workspace: Path) -> RunResult:
        """Create a validated JSON plan and execute it under the normal Harness controls."""
        planning_messages = (
            Message(
                role=MessageRole.SYSTEM,
                content=(
                    'Return only raw JSON matching {"steps":["step"]}. '
                    "Do not wrap it in a code fence and do not call tools. "
                    "Plan concrete verification steps."
                ),
            ),
            Message(role=MessageRole.USER, content=task),
        )
        messages = list(planning_messages)
        usage = Usage()
        # Bounded correction: a planner that keeps breaking the protocol must not
        # become a high-cost loop. Every attempt is charged in full, so a plan
        # recovered by retry stays visibly more expensive than one that worked
        # first time — correction is not free.
        attempt = 0
        while True:
            attempt += 1
            try:
                # Record the planner request so the projection can pair it with
                # the plan decision and derive planner latency, exactly as the
                # executor loop does for each model decision.
                self._trace.append(
                    "model.request",
                    {
                        "phase": "planner",
                        "task_id": task_id,
                        "tool_count": 0,
                        "attempt": attempt,
                        "correction": attempt > 1,
                    },
                )
                response = await self._planner.decide(
                    ModelRequest(task_id=task_id, messages=tuple(messages), tools=())
                )
            except Exception as exc:
                usage = self._charge(usage, steps=1)
                return self._planning_failure(
                    task_id,
                    tuple(messages),
                    f"planner failed: {exc}",
                    usage,
                    error_type="planner_failed",
                )
            usage = self._charge(
                usage,
                steps=1,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            if not isinstance(response.action, FinalAction):
                # A tool call is itself a protocol violation, so it earns the same
                # bounded correction rather than an immediate death.
                violation = PlanProtocolViolation(
                    "planner returned a tool call instead of a structured plan",
                    code="planner_called_tool",
                )
            else:
                try:
                    plan = _parse_plan(response.action.content)
                except PlanProtocolViolation as exc:
                    violation = exc
                else:
                    return await self._execute(
                        task_id=task_id,
                        task=task,
                        workspace=workspace,
                        plan=plan,
                        response=response,
                        usage=usage,
                    )
            self._trace.append(
                "plan.failed",
                {
                    "error_code": violation.code,
                    "error": str(violation),
                    "recoverable": True,
                    "occurrence": attempt,
                    "max_planner_retries": self._max_planner_retries,
                    # The raw planner output is recorded because it is the only way
                    # to replay this failure: a summary like "invalid plan" cannot be
                    # fed back to a model double, so without the response a replay
                    # could reproduce the failure but never test a fix for it.
                    "response": _planner_response_payload(response),
                },
            )
            if attempt > self._max_planner_retries:
                return self._planning_failure(
                    task_id,
                    tuple(messages),
                    f"repeated_planner_protocol_violation: {attempt} violations of "
                    f"{violation.code} exceeded the retry limit of "
                    f"{self._max_planner_retries}",
                    usage,
                    error_type="repeated_planner_protocol_violation",
                )
            # Feed the violation back without leaking the raw exception text, then
            # let the planner try again.
            messages.append(
                Message(role=MessageRole.USER, content=_plan_correction_message(violation))
            )

    def _charge(
        self,
        usage: Usage,
        *,
        steps: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Usage:
        """Add one attempt's cost so no planner attempt is silently free."""
        return Usage(
            steps=usage.steps + steps,
            tool_calls=usage.tool_calls,
            input_tokens=usage.input_tokens + input_tokens,
            output_tokens=usage.output_tokens + output_tokens,
        )

    async def _execute(
        self,
        *,
        task_id: str,
        task: str,
        workspace: Path,
        plan: ExecutionPlan,
        response: ModelResult,
        usage: Usage,
    ) -> RunResult:
        """Record the accepted plan and hand control to the executor loop."""
        self._trace.append(
            "plan.created",
            {
                "steps": list(plan.steps),
                # The planner's own usage is recorded on the phase event so the
                # projection can attribute the extra decision to planning rather
                # than inferring it from a step-count difference.
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "model_name": response.model_name,
                "planner_attempts": usage.steps,
            },
        )
        instruction = Message(
            role=MessageRole.SYSTEM,
            content=(
                "Follow this Harness-validated plan. Re-evaluate after every observation and "
                f"finish only with verification evidence. Plan: {plan.model_dump_json()}"
            ),
        )
        return await self._executor.run(
            task_id=task_id,
            task=task,
            workspace=workspace,
            instructions=(instruction,),
            initial_usage=usage,
        )

    def _planning_failure(
        self,
        task_id: str,
        messages: tuple[Message, ...],
        error: str,
        usage: Usage | None = None,
        *,
        error_type: str | None = None,
    ) -> RunResult:
        """End the run because planning could not produce an executable plan.

        The caller records the ``plan.failed`` evidence, so this method only builds
        the terminal result and never duplicates a less detailed event.
        """
        return RunResult(
            task_id=task_id,
            status=RunStatus.FAILED,
            messages=messages,
            usage=usage or Usage(),
            error=error,
        )

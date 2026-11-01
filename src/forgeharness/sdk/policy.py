"""Contextual policy and a backwards-compatible runtime binding."""

from typing import Protocol

from pydantic import ValidationError

from forgeharness.domain.models import FrozenModel, RunContext, ToolCall
from forgeharness.tools.base import ToolSpec
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.registry import ToolRegistry


class AuthorizationRequest(FrozenModel):
    """Normalized action and trusted host identity for current policy evaluation."""

    context: RunContext
    spec: ToolSpec
    call: ToolCall


class ContextualPolicy(Protocol):
    """Application-owned resource, role and live-version checks."""

    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision: ...

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool: ...


class BoundPolicy:
    """Supply trusted context to the neutral runtime's legacy policy protocol."""

    def __init__(
        self, policy: ContextualPolicy, context: RunContext, registry: ToolRegistry
    ) -> None:
        self._policy = policy
        self._context = context
        self._registry = registry

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        if task_id != self._context.run_id:
            return PolicyDecision(type=PolicyDecisionType.DENY, reason="run identity mismatch")
        tool = self._registry.get(call.name)
        if tool is None:
            return PolicyDecision(type=PolicyDecisionType.DENY, reason="unknown tool")
        try:
            arguments = tool.input_model.model_validate(call.arguments).model_dump(mode="json")
        except ValidationError:
            return PolicyDecision(
                type=PolicyDecisionType.DENY,
                reason="invalid tool arguments",
                rule="harness.invalid_arguments",
            )
        call = call.model_copy(update={"arguments": arguments})
        return self._policy.evaluate(
            AuthorizationRequest(context=self._context, spec=spec, call=call)
        )

"""Agent runtime, budgets, and loop strategies."""

from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.plan_execute import PlanExecuteRuntime
from forgeharness.runtime.subagent import SubAgentSpec, SubAgentTool
from forgeharness.runtime.verification import (
    NoopVerifier,
    ToolEvidence,
    VerificationRequest,
    VerificationResult,
    Verifier,
)

__all__ = [
    "AgentRuntime",
    "CodingVerifier",
    "NoopVerifier",
    "PlanExecuteRuntime",
    "SubAgentSpec",
    "SubAgentTool",
    "ToolEvidence",
    "VerificationRequest",
    "VerificationResult",
    "Verifier",
]


def __getattr__(name: str) -> object:
    """Keep the historical coding verifier export lazy until API v2."""
    from forgeharness.runtime import verification

    return getattr(verification, name)

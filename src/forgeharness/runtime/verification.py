"""Harness-owned verification of a model-claimed final answer.

A run ends when the model emits a final action. That is a claim, not evidence.
A verifier inspects structured tool results and workspace state the Harness
already controls, then either accepts the claim or describes the missing
evidence so the runtime can return it to the model. Verifiers never own loop
control: they only answer passed / reason / evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from forgeharness.domain.models import FinalAction, FrozenModel, RunContext
from forgeharness.domain.models import ToolEvidence as ToolEvidence


class VerificationRequest(FrozenModel):
    """Everything a verifier may inspect, without parsing chat text."""

    task_id: str
    final: FinalAction
    workspace: Path | None
    run_context: RunContext | None = None
    tool_results: tuple[ToolEvidence, ...] = ()


class VerificationResult(FrozenModel):
    """A verifier's verdict and the evidence behind it."""

    passed: bool
    reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()


class Verifier(Protocol):
    """Decide whether a final answer is supported by verifiable evidence."""

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        """Return a verdict; the runtime alone decides what happens next."""
        ...


class NoopVerifier:
    """Accept every final answer; wires the gate without adding a check."""

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        del request
        return VerificationResult(passed=True, reason="no verification configured")


def __getattr__(name: str) -> Any:
    """Compatibility for the pre-SDK verifier import; removed at API v2."""
    if name == "CodingVerifier":
        from importlib import import_module

        return import_module("forgeharness.coding.verification").CodingVerifier
    raise AttributeError(name)

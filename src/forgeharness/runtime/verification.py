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

from forgeharness.domain.models import FinalAction, FrozenModel


class ToolEvidence(FrozenModel):
    """Structured outcome of one executed tool call, captured by the runtime."""

    name: str
    ok: bool
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class VerificationRequest(FrozenModel):
    """Everything a verifier may inspect, without parsing chat text."""

    task_id: str
    final: FinalAction
    workspace: Path
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


class CodingVerifier:
    """Require a successful workspace edit and passing tests before accepting."""

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        """Check structured tool evidence, never message strings."""
        writes = [result for result in request.tool_results if result.name == "write_file"]
        failed_writes = tuple(result.name for result in writes if not result.ok)
        if failed_writes:
            return VerificationResult(
                passed=False,
                reason="a requested file write did not succeed",
                evidence=failed_writes,
            )
        if not any(result.ok for result in writes):
            return VerificationResult(
                passed=False,
                reason="no workspace change was made",
            )
        tests = [result for result in request.tool_results if result.name == "run_tests"]
        if not tests:
            return VerificationResult(
                passed=False,
                reason="no test evidence: run the test command before finishing",
            )
        last = tests[-1]
        if not last.ok:
            return VerificationResult(
                passed=False,
                reason="the test command did not pass",
                evidence=(f"tests: exit_code={last.metadata.get('exit_code')}",),
            )
        return VerificationResult(
            passed=True,
            reason="a workspace change was written and tests pass",
            evidence=(f"tests: exit_code={last.metadata.get('exit_code')}",),
        )

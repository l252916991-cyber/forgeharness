"""Deterministic tests for the CodingVerifier's structured evidence checks."""

from __future__ import annotations

from pathlib import Path

from forgeharness.domain.models import FinalAction
from forgeharness.runtime.verification import (
    CodingVerifier,
    ToolEvidence,
    VerificationRequest,
)


def request(tmp_path: Path, *evidence: ToolEvidence) -> VerificationRequest:
    return VerificationRequest(
        task_id="task-1",
        final=FinalAction(content="done"),
        workspace=tmp_path,
        tool_results=evidence,
    )


def write(ok: bool = True) -> ToolEvidence:
    return ToolEvidence(
        name="write_file",
        ok=ok,
        content="wrote 6 bytes to fixed.py" if ok else "file changed since it was read",
    )


def run_tests(exit_code: int) -> ToolEvidence:
    return ToolEvidence(
        name="run_tests",
        ok=exit_code == 0,
        content="",
        metadata={"exit_code": exit_code},
    )


async def test_passes_with_a_successful_write_and_passing_tests(tmp_path: Path) -> None:
    result = await CodingVerifier().verify(request(tmp_path, write(), run_tests(0)))

    assert result.passed
    assert "tests pass" in result.reason
    assert result.evidence == ("tests: exit_code=0",)


async def test_rejects_when_tests_were_never_run(tmp_path: Path) -> None:
    result = await CodingVerifier().verify(request(tmp_path, write()))

    assert not result.passed
    assert "no test evidence" in result.reason


async def test_rejects_failing_tests(tmp_path: Path) -> None:
    result = await CodingVerifier().verify(request(tmp_path, write(), run_tests(1)))

    assert not result.passed
    assert result.reason == "the test command did not pass"
    assert result.evidence == ("tests: exit_code=1",)


async def test_rejects_a_failed_write(tmp_path: Path) -> None:
    result = await CodingVerifier().verify(request(tmp_path, write(ok=False), run_tests(0)))

    assert not result.passed
    assert "write did not succeed" in result.reason
    assert result.evidence == ("write_file",)


async def test_rejects_when_the_workspace_is_unchanged(tmp_path: Path) -> None:
    result = await CodingVerifier().verify(request(tmp_path, run_tests(0)))

    assert not result.passed
    assert result.reason == "no workspace change was made"


async def test_uses_the_latest_test_result(tmp_path: Path) -> None:
    result = await CodingVerifier().verify(request(tmp_path, write(), run_tests(1), run_tests(0)))

    assert result.passed

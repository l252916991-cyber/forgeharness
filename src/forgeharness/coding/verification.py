"""Coding-specific completion checks."""

from forgeharness.sdk import VerificationRequest, VerificationResult


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
        last_write = max(
            i
            for i, item in enumerate(request.tool_results)
            if item.name == "write_file" and item.ok
        )
        last_test = max(
            i for i, item in enumerate(request.tool_results) if item.name == "run_tests"
        )
        if last_test < last_write:
            return VerificationResult(passed=False, reason="tests are stale after the last write")
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

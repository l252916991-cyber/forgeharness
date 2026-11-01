"""Real hosted core paths charge logical rejection without physical dispatch."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace

import pytest

from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.models.base import ModelRequest
from forgeharness.runtime.verification import VerificationRequest, VerificationResult
from forgeharness.tools.base import ToolErrorCode, ToolFailure

from .execution_fixtures import (
    SyntheticBusinessLedger,
    application_fixture,
    canonical,
    digest,
    hosted_registration,
)


@dataclass(frozen=True)
class RejectedRequest:
    cause: str
    tool: str

    @property
    def call(self) -> ToolCall:
        return ToolCall(
            id="rejected-request",
            name=self.tool,
            arguments={"record_key": 7}
            if self.cause == "invalid_arguments"
            else {"record_key": "record-1"},
        )

    @property
    def observation(self) -> Message:
        if self.cause == "policy_denied":
            content = "policy denied tool: synthetic owner revoked"
        else:
            unknown = self.cause == "unknown_tool"
            content = ToolFailure(
                code=ToolErrorCode.INVALID_ARGUMENTS,
                message="No such tool is registered."
                if unknown
                else "The arguments do not match the tool's required schema.",
                recoverable=True,
                details={"tool": self.tool} if unknown else {},
                suggestion="Choose one of the available tools."
                if unknown
                else "Send arguments that satisfy the tool's input schema.",
            ).as_observation()
        return Message(
            role=MessageRole.TOOL,
            tool_call_id=self.call.id,
            tool_name=self.tool,
            content=content,
        )

    @property
    def final(self) -> FinalAction:
        return FinalAction(
            content=canonical({"status": "rejected", "cause": self.cause, "tool": self.tool})
        )


class RejectionModel:
    """The provider records its own request/result before the Harness sees it."""

    def __init__(self, ledger: SyntheticBusinessLedger, rejected: RejectedRequest) -> None:
        self.ledger, self.rejected = ledger, rejected

    async def decide(self, request: ModelRequest) -> ModelResult:
        observed = any(message.role is MessageRole.TOOL for message in request.messages)
        result = ModelResult(
            action=self.rejected.final if observed else ToolAction(call=self.rejected.call),
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            model_name="synthetic-rejection-provider-v1",
        )
        return await asyncio.to_thread(self.ledger.model, request, result)


class RejectionVerifier:
    """Verify the rejection against provider history and independent business state."""

    def __init__(self, ledger: SyntheticBusinessLedger, rejected: RejectedRequest) -> None:
        self.ledger, self.rejected = ledger, rejected
        self.calls = 0

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        self.calls += 1
        receipts = await asyncio.to_thread(self.ledger.all_receipts, "model")
        tools = await asyncio.to_thread(self.ledger.all_receipts, "tool")
        record = await asyncio.to_thread(self.ledger.record, "orders")
        effects = await asyncio.to_thread(self.ledger.effect_count, "orders")
        passed = False
        if len(receipts) == 2:
            first, final = receipts
            observed = tuple(
                Message.model_validate(message)
                for message in final.request["messages"]
                if message["role"] == "tool"
            )
            passed = (
                request.run_context is not None
                and request.run_context.scope.tenant_id == "org-a"
                and first.request["task_id"] == final.request["task_id"] == request.task_id
                and first.result["action"]
                == ToolAction(call=self.rejected.call).model_dump(mode="json")
                and final.result["action"] == self.rejected.final.model_dump(mode="json")
                and observed == (self.rejected.observation,)
                and all(receipt.physical_fact is not None for receipt in receipts)
                and request.final == self.rejected.final
                and not request.tool_results
                and not tools
                and effects == 0
                and record["value"] == "draft"
                and record["revision"] == 1
            )
        return VerificationResult(
            passed=passed,
            reason="independent provider history and unchanged business state checked",
            evidence=("synthetic-ledger://logical-rejection",) if passed else (),
        )


@pytest.mark.parametrize(
    ("cause", "tool"),
    [
        ("policy_denied", "oracle.read"),
        ("unknown_tool", "oracle.missing"),
        ("invalid_arguments", "oracle.read"),
        ("invalid_arguments", "oracle.write"),
    ],
)
async def test_hosted_rejected_tool_request_finishes_with_exact_logical_usage(
    pgcase, tmp_path, cause, tool
):
    rejected = RejectedRequest(cause, tool)
    ledger = SyntheticBusinessLedger(tmp_path / "independent-provider.sqlite")
    ledger.seed("orders")
    fixture = application_fixture(ledger)
    fixture.policy.revoked = cause == "policy_denied"
    verifier = RejectionVerifier(ledger, rejected)
    fixture.application = replace(
        fixture.application,
        model_factory=lambda: RejectionModel(ledger, rejected),
        verifier=verifier,
        budget=fixture.application.budget.model_copy(update={"max_steps": 2}),
    )
    registration = replace(
        hosted_registration(fixture),
        release_digest=digest({"test": "hosted-rejection-v1", "cause": cause, "tool": tool}),
        model_binding_digest=digest({"model": "synthetic-rejection-provider-v1"}),
    )
    runtime = HostedRuntime(pgcase.store, {registration.deployment_id: registration})
    owner = await pgcase.seed("org-a", "owner", can_execute=False)
    worker = await pgcase.seed("org-a", "worker")
    initial = await runtime.accept(
        owner,
        registration.deployment_id,
        "rejection-case",
        "Handle this request within the registered tool and resource scope.",
    )

    finished = await runtime.advance(worker, initial.run.run_id)

    assert finished.run.status == "succeeded"
    assert finished.checkpoint.status is RunStatus.SUCCEEDED
    assert finished.checkpoint.final_output == rejected.final.content
    assert finished.checkpoint.pending_approval is None
    assert finished.checkpoint.authorized_call_id is None
    assert finished.checkpoint.usage == Usage(
        steps=2, tool_calls=1, input_tokens=6, output_tokens=4
    )
    assert finished.run.used_units == 10 and finished.run.reserved_units == 0
    assert verifier.calls == 1
    logical = await pgcase.admin.fetchrow("SELECT * FROM fh_cloud.execution_logical_usage")
    assert logical["cause"] == cause and logical["logical_id"] == rejected.call.id
    assert json.loads(logical["call"]) == rejected.call.model_dump(mode="json")
    assert json.loads(logical["observation"]) == rejected.observation.model_dump(mode="json")
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_logical_usage") == 1
    physical = await pgcase.admin.fetch(
        "SELECT c.kind,i.state,r.outcome FROM fh_cloud.execution_calls c "
        "JOIN fh_cloud.invocations i USING(org_id,run_id,invocation_id) "
        "JOIN fh_cloud.receipts r USING(org_id,run_id,invocation_id)"
    )
    assert len(physical) == 2
    assert all(
        dict(row) == {"kind": "model", "state": "settled", "outcome": "completed"}
        for row in physical
    )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.approvals") == 0
    assert not ledger.all_receipts("tool") and ledger.effect_count("orders") == 0
    assert all(receipt.dispatches == 1 for receipt in ledger.all_receipts("model"))
    trace = await runtime.trace(owner, finished.run.run_id)
    rejection_event = {
        "policy_denied": "policy.decided",
        "unknown_tool": "tool.unknown",
        "invalid_arguments": "tool.invalid_arguments",
    }[cause]
    assert sum(event.type == rejection_event for event in trace.events) == 1
    assert await runtime.advance(worker, finished.run.run_id) == finished
    assert len(ledger.all_receipts("model")) == 2
    assert all(receipt.dispatches == 1 for receipt in ledger.all_receipts("model"))

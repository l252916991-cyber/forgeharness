"""Keyless tests for runtime success, policy, and budget behavior."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from forgeharness.context.compression import ObservationCompressor
from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.verification import (
    ToolEvidence,
    VerificationRequest,
    VerificationResult,
    Verifier,
)
from forgeharness.state.approval import InMemoryApprovalLedger
from forgeharness.state.checkpoint import CheckpointConflict, SQLiteCheckpointStore
from forgeharness.tools.base import RiskLevel, ToolContext, ToolOutput, ToolSpec
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry


class EmptyInput(BaseModel):
    """No-argument input used by policy-path tools."""


class RiskTool:
    """A test tool whose risk controls the policy branch."""

    def __init__(self, name: str, risk: RiskLevel) -> None:
        self._name = name
        self._risk = risk

    @property
    def input_model(self) -> type[BaseModel]:
        return EmptyInput

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description="Exercise a policy branch.",
            input_schema=EmptyInput.model_json_schema(),
            risk=self._risk,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        return ToolOutput(ok=True, content="should not execute")


def build_runtime(
    model: ScriptedModel,
    trace: InMemoryTrace,
    budget: RunBudget | None = None,
    extra_tools: tuple[RiskTool, ...] = (),
    approval_ledger: InMemoryApprovalLedger | None = None,
    checkpoint_store: SQLiteCheckpointStore | None = None,
    observation_compressor: ObservationCompressor | None = None,
    verifier: Verifier | None = None,
) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(EchoTool())
    for tool in extra_tools:
        registry.register(tool)
    return AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=trace,
        budget=budget,
        approval_ledger=approval_ledger,
        checkpoint_store=checkpoint_store,
        observation_compressor=observation_compressor,
        verifier=verifier,
    )


async def test_runtime_executes_tool_and_returns_final_answer(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(id="call-1", name="echo", arguments={"text": "observed"})
                ),
                model_name="scripted",
            ),
            ModelResult(action=FinalAction(content="done"), model_name="scripted"),
        ]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace).run(
        task_id="task-1", task="echo a value", workspace=tmp_path
    )

    assert result.status == RunStatus.SUCCEEDED
    assert result.final_output == "done"
    assert result.usage.steps == 2
    assert result.usage.tool_calls == 1
    assert model.requests[1].messages[-1].content == "observed"
    assert [event.type for event in trace.events] == [
        "run.started",
        "model.request",
        "model.action",
        "policy.decided",
        "tool.completed",
        "model.request",
        "model.action",
        "run.finished",
    ]


async def test_runtime_exhausts_step_budget(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(id="call-1", name="echo", arguments={"text": "once"})
                )
            )
        ]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace, RunBudget(max_steps=1)).run(
        task_id="task-1", task="never finish", workspace=tmp_path
    )

    assert result.status == RunStatus.EXHAUSTED
    assert result.error == "step budget exhausted"
    assert result.usage.tool_calls == 1


async def test_runtime_turns_model_failure_into_failed_result(tmp_path: Path) -> None:
    model = ScriptedModel([])
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace).run(
        task_id="task-1", task="model will fail", workspace=tmp_path
    )

    assert result.status == RunStatus.FAILED
    assert result.error == "model failed: RuntimeError: scripted model has no response remaining"
    assert [event.type for event in trace.events] == [
        "run.started",
        "model.request",
        "model.failed",
        "run.finished",
    ]


async def test_runtime_rejects_non_system_instructions(tmp_path: Path) -> None:
    runtime = build_runtime(ScriptedModel([]), InMemoryTrace("task-1"))

    with pytest.raises(ValueError, match="must be system messages"):
        await runtime.run(
            task_id="task-1",
            task="task",
            workspace=tmp_path,
            instructions=(Message(role=MessageRole.USER, content="not an instruction"),),
        )


async def test_runtime_exhausts_token_budget_after_model_response(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(
                action=FinalAction(content="must not be accepted"),
                usage=ModelUsage(input_tokens=10),
            )
        ]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace, RunBudget(max_input_tokens=10)).run(
        task_id="task-1", task="consume the budget", workspace=tmp_path
    )

    assert result.status == RunStatus.EXHAUSTED
    assert result.error == "token budget exhausted"
    assert result.final_output is None


async def test_runtime_reports_unknown_tool_to_model(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(action=ToolAction(call=ToolCall(id="call-1", name="missing"))),
            ModelResult(action=FinalAction(content="recovered")),
        ]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace).run(
        task_id="task-1", task="request an unknown tool", workspace=tmp_path
    )

    assert result.status == RunStatus.SUCCEEDED
    assert model.requests[1].messages[-1].content == "unknown tool: missing"
    assert "tool.unknown" in [event.type for event in trace.events]


async def test_runtime_suspends_write_for_approval(tmp_path: Path) -> None:
    model = ScriptedModel(
        [ModelResult(action=ToolAction(call=ToolCall(id="call-1", name="writer")))]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(
        model, trace, extra_tools=(RiskTool("writer", RiskLevel.WRITE),)
    ).run(task_id="task-1", task="write something", workspace=tmp_path)

    assert result.status == RunStatus.AWAITING_APPROVAL
    assert result.pending_approval is not None
    assert result.pending_approval.call.name == "writer"
    assert result.usage.tool_calls == 0


async def test_runtime_resumes_after_exact_approval(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(action=ToolAction(call=ToolCall(id="call-1", name="writer"))),
            ModelResult(action=FinalAction(content="write reviewed")),
        ]
    )
    trace = InMemoryTrace("task-1")
    ledger = InMemoryApprovalLedger()
    runtime = build_runtime(
        model,
        trace,
        extra_tools=(RiskTool("writer", RiskLevel.WRITE),),
        approval_ledger=ledger,
    )
    suspended = await runtime.run(task_id="task-1", task="write something", workspace=tmp_path)
    assert suspended.pending_approval is not None
    grant = ledger.issue(
        task_id="task-1",
        call=suspended.pending_approval.call,
        granted_by="interviewer",
    )

    result = await runtime.resume_approved(previous=suspended, grant=grant, workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    assert result.final_output == "write reviewed"
    assert result.usage.tool_calls == 1
    assert model.requests[1].messages[-1].content == "should not execute"
    assert "approval.consumed" in [event.type for event in trace.events]


async def test_runtime_rejects_resume_without_pending_approval(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResult(action=FinalAction(content="done"))])
    trace = InMemoryTrace("task-1")
    ledger = InMemoryApprovalLedger()
    runtime = build_runtime(model, trace, approval_ledger=ledger)
    finished = await runtime.run(task_id="task-1", task="finish", workspace=tmp_path)
    grant = ledger.issue(task_id="task-1", call=ToolCall(id="call", name="echo"), granted_by="user")

    with pytest.raises(ValueError, match="awaiting-approval"):
        await runtime.resume_approved(previous=finished, grant=grant, workspace=tmp_path)


async def test_runtime_persists_and_resumes_approval_checkpoint(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(action=ToolAction(call=ToolCall(id="call-1", name="writer"))),
            ModelResult(action=FinalAction(content="persisted")),
        ]
    )
    trace = InMemoryTrace("task-1")
    ledger = InMemoryApprovalLedger()
    store = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    runtime = build_runtime(
        model,
        trace,
        extra_tools=(RiskTool("writer", RiskLevel.WRITE),),
        approval_ledger=ledger,
        checkpoint_store=store,
    )

    suspended = await runtime.run(task_id="task-1", task="write", workspace=tmp_path)
    assert suspended.pending_approval is not None
    assert store.load("task-1") == suspended
    grant = ledger.issue(task_id="task-1", call=suspended.pending_approval.call, granted_by="user")
    finished = await runtime.resume_approved(previous=suspended, grant=grant, workspace=tmp_path)

    assert finished.status == RunStatus.SUCCEEDED
    assert store.load("task-1") == finished
    assert finished.checkpoint_revision == 5


async def test_runtime_rejects_resume_of_stale_checkpoint(tmp_path: Path) -> None:
    model = ScriptedModel(
        [ModelResult(action=ToolAction(call=ToolCall(id="call-1", name="writer")))]
    )
    trace = InMemoryTrace("task-1")
    ledger = InMemoryApprovalLedger()
    store = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    runtime = build_runtime(
        model,
        trace,
        extra_tools=(RiskTool("writer", RiskLevel.WRITE),),
        approval_ledger=ledger,
        checkpoint_store=store,
    )
    suspended = await runtime.run(task_id="task-1", task="write", workspace=tmp_path)
    assert suspended.pending_approval is not None
    store.save(suspended)
    grant = ledger.issue(task_id="task-1", call=suspended.pending_approval.call, granted_by="user")

    with pytest.raises(CheckpointConflict, match="not the current checkpoint"):
        await runtime.resume_approved(previous=suspended, grant=grant, workspace=tmp_path)


async def test_runtime_returns_policy_denial_to_model(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(action=ToolAction(call=ToolCall(id="call-1", name="process"))),
            ModelResult(action=FinalAction(content="used a safer approach")),
        ]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(
        model, trace, extra_tools=(RiskTool("process", RiskLevel.PROCESS),)
    ).run(task_id="task-1", task="run a process", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    assert model.requests[1].messages[-1].content is not None
    assert model.requests[1].messages[-1].content.startswith("policy denied tool:")


async def test_runtime_checks_tool_budget_before_dispatch(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(id="call-1", name="echo", arguments={"text": "blocked"})
                )
            )
        ]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace, RunBudget(max_tool_calls=0)).run(
        task_id="task-1", task="call a tool", workspace=tmp_path
    )

    assert result.status == RunStatus.EXHAUSTED
    assert result.error == "tool-call budget exhausted"
    assert result.usage.tool_calls == 0


async def test_runtime_compresses_model_visible_observations(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(id="call-1", name="echo", arguments={"text": "x" * 500})
                )
            ),
            ModelResult(action=FinalAction(content="done")),
        ]
    )
    runtime = build_runtime(
        model,
        InMemoryTrace("task-1"),
        observation_compressor=ObservationCompressor(max_chars=200),
    )

    result = await runtime.run(task_id="task-1", task="compress", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    observation = model.requests[1].messages[-1].content
    assert observation is not None
    assert observation.startswith("[compressed observation:")
    assert len(observation) <= 200


async def test_runtime_compacts_history_to_the_context_window(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            *(
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(
                            id=f"call-{index}", name="echo", arguments={"text": "x" * 200}
                        )
                    )
                )
                for index in range(3)
            ),
            ModelResult(action=FinalAction(content="done")),
        ]
    )
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace, RunBudget(max_context_tokens=200)).run(
        task_id="task-1", task="use several tools", workspace=tmp_path
    )

    assert result.status == RunStatus.SUCCEEDED
    assert "context.compacted" in [event.type for event in trace.events]
    # The checkpoint keeps the full transcript while the model sees a bounded window.
    assert len(result.messages) == 8
    assert len(model.requests[-1].messages) < len(result.messages)
    sent = model.requests[-1].messages
    assert sent[0].role == MessageRole.SYSTEM
    assert sent[0].content is not None
    assert sent[0].content.startswith("[compacted context:")
    assert sent[-1] == result.messages[-2]


async def test_runtime_fails_when_context_window_cannot_fit_required_messages(
    tmp_path: Path,
) -> None:
    model = ScriptedModel([ModelResult(action=FinalAction(content="unused"))])
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace, RunBudget(max_context_tokens=1)).run(
        task_id="task-1", task="t" * 4_000, workspace=tmp_path
    )

    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("context budget exceeded:")
    assert "context.overflow" in [event.type for event in trace.events]


class RecordingVerifier:
    """Accept or reject on a script while capturing what it was shown."""

    def __init__(self, verdicts: list[VerificationResult]) -> None:
        self._verdicts = verdicts
        self.requests: list[VerificationRequest] = []

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        self.requests.append(request)
        return self._verdicts.pop(0)


class ExplodingVerifier:
    """Raise to prove a broken verifier fails the run instead of passing it."""

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        del request
        raise RuntimeError("verifier is broken")


async def test_runtime_accepts_verified_final_answer(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(id="call-1", name="echo", arguments={"text": "observed"})
                )
            ),
            ModelResult(action=FinalAction(content="done")),
        ]
    )
    trace = InMemoryTrace("task-1")
    verifier = RecordingVerifier([VerificationResult(passed=True, reason="evidence present")])

    result = await build_runtime(model, trace, verifier=verifier).run(
        task_id="task-1", task="verify me", workspace=tmp_path
    )

    assert result.status == RunStatus.SUCCEEDED
    assert result.final_output == "done"
    # The verifier sees structured evidence, not the chat transcript.
    assert verifier.requests[0].tool_results == (
        ToolEvidence(name="echo", ok=True, content="observed"),
    )
    assert "verification.passed" in [event.type for event in trace.events]


async def test_runtime_returns_rejected_verdict_to_model_and_retries(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(action=FinalAction(content="premature")),
            ModelResult(action=FinalAction(content="supported")),
        ]
    )
    trace = InMemoryTrace("task-1")
    verifier = RecordingVerifier(
        [
            VerificationResult(
                passed=False, reason="no test evidence", evidence=("run_tests: missing",)
            ),
            VerificationResult(passed=True, reason="tests pass"),
        ]
    )

    result = await build_runtime(model, trace, verifier=verifier).run(
        task_id="task-1", task="finish with evidence", workspace=tmp_path
    )

    assert result.status == RunStatus.SUCCEEDED
    assert result.final_output == "supported"
    feedback = model.requests[1].messages[-1]
    assert feedback.role == MessageRole.USER
    assert feedback.content is not None
    assert "Harness verification rejected" in feedback.content
    assert "no test evidence" in feedback.content
    assert "run_tests: missing" in feedback.content
    assert [event.type for event in trace.events].count("verification.rejected") == 1


async def test_runtime_exhausts_budget_when_rejection_never_resolves(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResult(action=FinalAction(content="attempt one")),
            ModelResult(action=FinalAction(content="attempt two")),
        ]
    )
    trace = InMemoryTrace("task-1")
    verifier = RecordingVerifier(
        [
            VerificationResult(passed=False, reason="still unproven"),
            VerificationResult(passed=False, reason="still unproven"),
        ]
    )

    result = await build_runtime(model, trace, RunBudget(max_steps=2), verifier=verifier).run(
        task_id="task-1", task="never proven", workspace=tmp_path
    )

    assert result.status == RunStatus.EXHAUSTED
    assert result.final_output is None
    assert [event.type for event in trace.events].count("verification.rejected") == 2


async def test_runtime_fails_the_run_when_verifier_raises(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResult(action=FinalAction(content="unverified"))])
    trace = InMemoryTrace("task-1")

    result = await build_runtime(model, trace, verifier=ExplodingVerifier()).run(
        task_id="task-1", task="verify", workspace=tmp_path
    )

    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("verification failed: RuntimeError: verifier is broken")
    assert "verification.failed" in [event.type for event in trace.events]

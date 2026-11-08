"""Fan out many independent materials over bounded governed runs, then merge in code.

The long-context study (reports/long-context-scaling/REPORT.md) found that one model
call over about 200k tokens of independent materials starts to miss items, and that
splitting them into small groups restores accuracy. Plain program fan-out matched
fixed-specialist collaboration there at a third of the cost: when the answer is just
the union of per-group answers, no coordinator model is needed.

This is that pattern with the SDK's public contracts:

- each group is one ordinary governed run (its own state, budget and hash-chained
  trace) that reads its materials through a read-only tool and makes one model call;
- at most ``IN_FLIGHT`` groups run at once;
- the program merges the answers, and a group that failed is reported with the
  materials it left unchecked, never silently dropped.

No model, network or credentials: a scripted model stands in. Its "provider" refuses
one group with HTTP 402 (ADR 050), so the report shows a failed group.
Run ``uv run --no-sync python examples/fanout_demo.py``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    AuthorizationRequest,
    FinalAction,
    Harness,
    Message,
    MessageRole,
    ModelRequest,
    ModelRequestRejected,
    ModelResult,
    ModelUsage,
    PolicyDecision,
    PolicyDecisionType,
    ResourceScope,
    RiskLevel,
    RunBudget,
    RunContext,
    RunKernel,
    RunStatus,
    ToolCall,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    VerificationRequest,
    VerificationResult,
    verify_trace,
)

GROUP_SIZE = 25
IN_FLIGHT = 2
REFUSED_RECORD = "r-051"  # the scripted provider refuses the group holding this record
RULE = "A record violates the rule when its value is greater than its limit."
ANSWER = 'Return only JSON: {"violations": ["r-001", ...]}, or [] if none.'


def records() -> dict[str, str]:
    """One hundred synthetic records; values are a permutation of 0-99, so 49 violate."""
    return {
        f"r-{i:03d}": f"record r-{i:03d}: limit=50 value={(i * 37) % 100}" for i in range(1, 101)
    }


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str


class ReadRecord:
    input_model = ReadInput
    spec = ToolSpec(
        name="read_record",
        description="Read one synthetic record by its ID.",
        input_schema=ReadInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del context
        key = ReadInput.model_validate(arguments).key
        return ToolOutput(ok=key in self.values, content=self.values.get(key, "unknown ID"))


class ReadOnly:
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        allowed = request.spec.risk is RiskLevel.READ
        return PolicyDecision(
            type=PolicyDecisionType.ALLOW if allowed else PolicyDecisionType.DENY,
            reason="read-only fan-out demo",
        )

    def can_approve(self, **kwargs: object) -> bool:
        return False


class AnswerShape:
    """Completion needs evidence: the final answer must parse as the declared JSON."""

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        try:
            value = json.loads(request.final.content)
            ok = isinstance(value, dict) and isinstance(value.get("violations"), list)
        except ValueError:
            ok = False
        return VerificationResult(passed=ok, reason="answer shape checked")


class GroupAudit:
    """Read the group's records through the governed tool, then one model call."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    async def run(self, *, kernel: RunKernel, task: str) -> Any:
        texts = {}
        for index, key in enumerate(self.keys):
            output = await kernel.call_tool(
                ToolCall(id=f"read-{index}", name="read_record", arguments={"key": key})
            )
            if not output.ok:
                return await kernel.finish(status=RunStatus.FAILED, error="record read failed")
            texts[key] = output.content
        result = await kernel.call_model(
            messages=(
                Message(role=MessageRole.SYSTEM, content=RULE + "\n" + ANSWER),
                Message(role=MessageRole.USER, content=json.dumps({"records": texts})),
            ),
            phase="group-audit",
            include_tools=False,
        )
        assert isinstance(result.action, FinalAction)
        return await kernel.finish(final=result.action)


class ScriptedModel:
    """Stands in for a provider: applies the rule, and refuses one group with HTTP 402."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        shown = json.loads(request.messages[-1].content or "{}")["records"]
        if REFUSED_RECORD in shown:
            raise ModelRequestRejected(402)
        flagged = []
        for key, text in shown.items():
            fields = dict(part.split("=") for part in text.split(": ", 1)[1].split())
            if int(fields["value"]) > int(fields["limit"]):
                flagged.append(key)
        return ModelResult(
            action=FinalAction(content=json.dumps({"violations": flagged})),
            usage=ModelUsage(input_tokens=40 * len(shown), output_tokens=5 * len(flagged) + 5),
        )


def context(group: int) -> RunContext:
    return RunContext(
        application_id="fanout-demo",
        application_version="1",
        policy_version="read-only-v1",
        run_id=f"group-{group}",
        session_id="fanout-demo",
        correlation_id="fanout-demo",
        actor=Actor(id="demo"),
        scope=ResourceScope(tenant_id="local"),
    )


async def audit_group(
    number: int, keys: list[str], values: dict[str, str], root: Path
) -> dict[str, Any]:
    spec = ApplicationSpec(
        id="fanout-demo",
        version="1",
        policy_version="read-only-v1",
        model_factory=ScriptedModel,
        tools=(ReadRecord(values),),
        policy=ReadOnly(),
        verifier=AnswerShape(),
        budget=RunBudget(
            max_steps=2,
            max_tool_calls=len(keys),
            max_input_tokens=20_000,
            max_output_tokens=1_000,
            max_attempts_per_call=1,
        ),
    )
    harness = Harness(spec, state_dir=root / f"group-{number}", orchestrator=GroupAudit(keys))
    result = await harness.run(context=context(number), task=f"Audit {len(keys)} records.")
    answer = None
    if result.status is RunStatus.SUCCEEDED and result.final_output is not None:
        answer = json.loads(result.final_output)["violations"]
    return {
        "group": number,
        "records": keys,
        "status": result.status.value,
        "violations": answer,
        "trace_valid": verify_trace(harness.trace_path(context(number))).valid,
    }


async def fan_out(root: Path) -> dict[str, Any]:
    values = records()
    keys = list(values)
    groups = [keys[start : start + GROUP_SIZE] for start in range(0, len(keys), GROUP_SIZE)]
    gate = asyncio.Semaphore(IN_FLIGHT)

    async def bounded(number: int, group: list[str]) -> dict[str, Any]:
        async with gate:
            return await audit_group(number, group, values, root)

    results = await asyncio.gather(*(bounded(n, g) for n, g in enumerate(groups, start=1)))
    failed = [r for r in results if r["violations"] is None]
    return {
        "groups": len(results),
        "failed_groups": [r["group"] for r in failed],
        "unchecked_records": sorted(key for r in failed for key in r["records"]),
        "violations": sorted(v for r in results for v in r["violations"] or []),
        "traces_valid": all(r["trace_valid"] for r in results),
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        summary = asyncio.run(fan_out(Path(directory)))
    print(f"groups: {summary['groups']}, failed: {summary['failed_groups']}")
    print(f"violations found: {len(summary['violations'])}")
    print(f"unchecked because their group failed: {len(summary['unchecked_records'])} records")
    print(f"all traces verify: {summary['traces_valid']}")


if __name__ == "__main__":
    main()

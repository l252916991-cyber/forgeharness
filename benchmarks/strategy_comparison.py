"""Three strategy arms over the existing Harness/kernel and hash-chained trace.

Run dev first, then seal a protocol before evaluating. All scheduled failures are
retained. This is a small synthetic diagnosis experiment, not native framework
comparison, repository repair, or business qualification.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import random
import statistics
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from benchmarks.framework_snapshot import snapshot
from evals.strategy_cases import DEV, EVALUATION, Case
from forgeharness.models.openai_compatible import OpenAICompatibleConfig, OpenAICompatibleModel
from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    AuthorizationRequest,
    FinalAction,
    Harness,
    Message,
    MessageRole,
    ModelRequest,
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
    ToolAction,
    ToolCall,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    VerificationRequest,
    VerificationResult,
    verify_trace,
)

ROOT = Path(__file__).resolve().parents[1]
ARMS = ("single", "fixed", "multi")
BUDGET = RunBudget(
    max_steps=12,
    max_tool_calls=8,
    max_input_tokens=24000,
    max_output_tokens=3072,
    max_context_tokens=8192,
    reserve_output_tokens=256,
    max_wall_seconds=120,
    max_attempts_per_call=1,
)
SYSTEM = 'Solve the development diagnosis task. Final output must be JSON: {"answer":"..."}.'


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str


class Materials:
    input_model = ReadInput
    spec = ToolSpec(
        name="read_material",
        description="Read a task material by its listed key.",
        input_schema=ReadInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    def __init__(self, case: Case):
        self.values = {str(i): text for i, text in enumerate(case.materials)}
        self.calls: list[str] = []

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del context
        key = arguments.model_dump()["key"]
        self.calls.append(key)
        return ToolOutput(
            ok=key in self.values, content=self.values.get(key, "unknown material key")
        )


class Policy:
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        return PolicyDecision(
            type=PolicyDecisionType.ALLOW
            if request.spec.risk is RiskLevel.READ
            else PolicyDecisionType.DENY,
            reason="synthetic read-only experiment",
        )

    def can_approve(self, **kwargs: object) -> bool:
        return False


class ExactAnswer:
    def __init__(self, answer: str):
        self.answer = answer

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        try:
            passed = json.loads(request.final.content) == {"answer": self.answer}
        except ValueError:
            passed = False
        return VerificationResult(passed=passed, reason="external exact structured-answer check")


class ContractModel:
    """Oracle for wiring tests only; never qualifies as a strategy measurement."""

    def __init__(self, case: Case):
        self.case = case

    async def decide(self, request: ModelRequest) -> ModelResult:
        text = request.messages[-1].content or ""
        if "PLANNER_KEYS" in text:
            action = FinalAction(
                content=json.dumps({"keys": list(map(str, range(len(self.case.materials))))})
            )
        elif (
            "Available material keys:" in text
            and not text.startswith("{")
            and not any(m.role is MessageRole.TOOL for m in request.messages)
        ):
            action = ToolAction(
                call=ToolCall(id="read-0", name="read_material", arguments={"key": "0"})
            )
        else:
            action = FinalAction(content=json.dumps({"answer": self.case.answer}))
        return ModelResult(action=action, usage=ModelUsage(input_tokens=10, output_tokens=5))


class Flow:
    def __init__(self, arm: str, keys: list[str]):
        self.arm, self.keys = arm, keys

    async def run(self, *, kernel: RunKernel, task: str):
        keys = self.keys
        if self.arm == "multi":
            plan = await kernel.call_model(
                messages=(
                    Message(
                        role=MessageRole.SYSTEM,
                        content="Plan material reading; do not solve the task.",
                    ),
                    Message(
                        role=MessageRole.USER,
                        content=task
                        + '\nPLANNER_KEYS: return only JSON {"keys": [...]}; '
                        + "include each available key exactly once.",
                    ),
                ),
                phase="planner",
                include_tools=False,
            )
            if not isinstance(plan.action, FinalAction):
                return await kernel.finish(
                    status=RunStatus.FAILED, error="planner returned tool action"
                )
            try:
                parsed = json.loads(plan.action.content)
                keys = parsed["keys"]
                if not isinstance(keys, list) or Counter(keys) != Counter(self.keys):
                    raise ValueError("plan must be a permutation of material keys")
            except (ValueError, KeyError, TypeError):
                return await kernel.finish(
                    status=RunStatus.FAILED, error="invalid planner key list"
                )
            kernel.emit("strategy.plan", {"keys": keys, "content": plan.action.content})
        materials = {}
        for i, key in enumerate(keys):
            output = await kernel.call_tool(
                ToolCall(id=f"read-{i}", name="read_material", arguments={"key": key})
            )
            if not output.ok:
                return await kernel.finish(status=RunStatus.FAILED, error="material read failed")
            materials[key] = output.content
        messages = (
            Message(role=MessageRole.SYSTEM, content=SYSTEM),
            Message(
                role=MessageRole.USER, content=json.dumps({"task": task, "materials": materials})
            ),
        )
        result = await kernel.call_model(
            messages=messages,
            phase="worker" if self.arm == "multi" else "fixed",
            include_tools=False,
        )
        if self.arm == "multi":
            if not isinstance(result.action, FinalAction):
                return await kernel.finish(
                    status=RunStatus.FAILED, error="worker returned tool action"
                )
            result = await kernel.call_model(
                messages=(
                    *messages,
                    Message(role=MessageRole.ASSISTANT, content=result.action.content),
                    Message(
                        role=MessageRole.USER,
                        content="Review the proposed answer against all materials. "
                        'Correct if needed; return only JSON {"answer":"..."}.',
                    ),
                ),
                phase="reviewer",
                include_tools=False,
            )
        if not isinstance(result.action, FinalAction):
            return await kernel.finish(
                status=RunStatus.FAILED, error="final role returned tool action"
            )
        return await kernel.finish(final=result.action)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)]


def cluster_interval(rows: list[dict], arm: str, baseline: str | None = None) -> list[float]:
    ids = sorted({row["case"] for row in rows})
    means = {}
    for case in ids:

        def mean(a, case=case):
            return statistics.mean(
                float(r["success"]) for r in rows if r["case"] == case and r["arm"] == a
            )

        means[case] = mean(arm) - (mean(baseline) if baseline else 0)
    rng = random.Random(20261026)
    estimates = sorted(statistics.mean(means[rng.choice(ids)] for _ in ids) for _ in range(2000))
    return [estimates[49], estimates[1949]]


def summarize(rows: list[dict], expected: int, mode: str) -> dict:
    complete = (
        len(rows) == expected
        and len({(r["case"], r["arm"], r["sample"]) for r in rows}) == expected
        and all(sum(r["arm"] == arm for r in rows) == expected // 3 for arm in ARMS)
    )
    arms = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        success = sum(row["success"] for row in selected)
        known = mode == "local" and all(row["usage_known"] for row in selected)
        tokens = sum(
            row["usage"].get("input_tokens", 0) + row["usage"].get("output_tokens", 0)
            for row in selected
        )
        arms[arm] = {
            "runs": len(selected),
            "successes": success,
            "success_rate": success / len(selected),
            "task_cluster_success_95_interval": cluster_interval(rows, arm),
            "families": {
                family: {
                    "runs": sum(r["family"] == family for r in selected),
                    "successes": sum(r["success"] for r in selected if r["family"] == family),
                }
                for family in sorted({r["family"] for r in selected})
            },
            "latency_p50_seconds": percentile([r["seconds"] for r in selected], 0.5),
            "latency_p95_seconds": percentile([r["seconds"] for r in selected], 0.95),
            "input_tokens": sum(r["usage"].get("input_tokens", 0) for r in selected)
            if known
            else None,
            "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in selected)
            if known
            else None,
            "tokens_per_success_including_failures": tokens / success
            if success and known
            else None,
            "duplicate_tool_calls": sum(r["duplicate_tool_calls"] for r in selected),
            "human_intervention_rate": sum(r["human_intervention"] for r in selected)
            / len(selected),
            "usage_known": known,
        }
    return {
        "schema_version": 1,
        "mode": mode,
        "token_source": "provider_reported" if mode == "local" else "synthetic",
        "cost_metrics_valid": mode == "local"
        and all(r["usage_known"] for r in rows)
        and not any(r.get("infrastructure_error", False) for r in rows),
        "expected_runs": expected,
        "actual_runs": len(rows),
        "complete": complete,
        "strategy_measurement": (
            mode == "local"
            and complete
            and not any(r.get("infrastructure_error", False) for r in rows)
            and all(r["trace_valid"] for r in rows)
        ),
        "infrastructure_failures": sum(r.get("infrastructure_error", False) for r in rows),
        "arms": arms,
        "paired_multi_minus_single_success_95_interval": cluster_interval(rows, "multi", "single"),
        "paired_multi_minus_fixed_success_95_interval": cluster_interval(rows, "multi", "fixed"),
        "cost_unit": "provider-reported tokens; no API fee; energy and hardware cost unmeasured",
        "core_advantage_proven": False,
        "business_qualified": False,
        "limits": [
            "author-generated short synthetic diagnosis tasks, not repository repair",
            "no human quality rating or independent unseen task set",
            "kernel serializes calls; no parallel throughput claim",
            "one local model; model ID is not proof of upstream revision",
        ],
    }


def identity() -> dict:
    return {
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256((ROOT / "evals/strategy_cases.py").read_bytes()).hexdigest(),
        "budget": BUDGET.model_dump(mode="json"),
    }


def model_files(model: str) -> list[Path]:
    directory = Path.home() / ".omlx/models"
    selected = (directory / model).resolve()
    if not selected.is_relative_to(directory.resolve()):
        raise ValueError("model must belong to the existing local model directory")
    return sorted(p for p in selected.iterdir() if p.suffix in {".json", ".safetensors", ".model"})


def model_artifacts(model: str) -> dict:
    files = model_files(model)
    if not any(p.suffix == ".safetensors" for p in files):
        raise ValueError("cannot pin local model weights")
    result = {}
    for path in files:
        with path.open("rb") as stream:
            result[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


async def readiness(model: str) -> dict:
    try:
        required = sum(p.stat().st_size for p in model_files(model) if p.suffix == ".safetensors")
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.get("http://127.0.0.1:8000/health")
            response.raise_for_status()
            health = response.json()
        ceiling = health["engine_pool"]["final_ceiling"]
        return {
            "ready": required > 0 and ceiling >= required,
            "weight_bytes": required,
            "dynamic_ceiling_bytes": ceiling,
            "reason": "existing OMLX memory admission ceiling; no server settings changed",
        }
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        return {"ready": False, "reason": type(exc).__name__}


async def run_one(case: Case, arm: str, sample: int, args, destination: Path) -> dict:
    tool = Materials(case)
    known = args.mode == "contract"
    responses = []
    infrastructure_error = False
    requests_sent = 0

    async def inspect_request(request: httpx.Request) -> None:
        nonlocal requests_sent
        requests_sent += 1

    async def inspect_response(response: httpx.Response) -> None:
        nonlocal known, infrastructure_error
        infrastructure_error = infrastructure_error or not response.is_success
        await response.aread()
        try:
            body = response.json()
            usage = body.get("usage") or {}
            valid = all(
                type(usage.get(k)) is int and usage[k] >= 0
                for k in ("prompt_tokens", "completion_tokens")
            )
            responses.append(
                {
                    "model": body.get("model"),
                    "usage_complete": valid,
                    "status_code": response.status_code,
                }
            )
            known = all(r["usage_complete"] for r in responses)
        except (ValueError, AttributeError):
            known = False

    client = httpx.AsyncClient(
        trust_env=False,
        timeout=60,
        event_hooks={"request": [inspect_request], "response": [inspect_response]},
    )
    model = (
        ContractModel(case)
        if args.mode == "contract"
        else OpenAICompatibleModel(
            OpenAICompatibleConfig(
                base_url="http://127.0.0.1:8000/v1",
                api_key="local-no-secret",
                model=args.model,
                temperature=0,
                max_tokens=256,
                enable_thinking=False,
                timeout_seconds=60,
            ),
            client=client,
        )
    )
    app = ApplicationSpec(
        id="strategy-study",
        version="1",
        policy_version="1",
        model_factory=lambda: model,
        tools=(tool,),
        policy=Policy(),
        verifier=ExactAnswer(case.answer),
        budget=BUDGET,
        instructions=(SYSTEM,),
    )
    context = RunContext(
        application_id=app.id,
        application_version="1",
        policy_version="1",
        run_id=f"{case.id}-{arm}-{sample}",
        session_id="study",
        correlation_id="study",
        actor=Actor(id="local-study"),
        scope=ResourceScope(tenant_id="synthetic"),
    )
    harness = Harness(
        app,
        state_dir=destination / "state",
        orchestrator=None if arm == "single" else Flow(arm, list(tool.values)),
    )
    started = time.monotonic()
    usage, error, success = {}, None, False
    trace_valid = False
    try:
        result = await harness.run(
            context=context,
            task=case.question + "\nAvailable material keys: " + json.dumps(list(tool.values)),
        )
        usage = result.usage.model_dump(mode="json")
        success = result.status is RunStatus.SUCCEEDED
        error = result.error
        trace_valid = verify_trace(harness.trace_path(context)).valid
    except Exception as exc:
        # Store a bounded exception class, never provider bodies or credentials.
        error = type(exc).__name__
        known = False
        infrastructure_error = True
    finally:
        await client.aclose()
    row = {
        "case": case.id,
        "family": case.family,
        "arm": arm,
        "sample": sample,
        "success": success and trace_valid,
        "trace_valid": trace_valid,
        "error": error,
        "usage": usage,
        "usage_known": known and bool(usage),
        "seconds": time.monotonic() - started,
        "duplicate_tool_calls": len(tool.calls) - len(set(tool.calls)),
        "human_intervention": False,
        "provider_responses": responses,
        "usage_source": "synthetic" if args.mode == "contract" else "provider_reported",
        "infrastructure_error": infrastructure_error or requests_sent > len(responses),
    }
    write(destination / "result.json", row)
    return row


async def execute(args) -> None:
    if not args.output.resolve().is_relative_to(ROOT / "reports"):
        raise ValueError("output must be inside workspace reports")
    if args.output.exists():
        raise ValueError("output already exists; preserve attempts and choose a new directory")
    protocol = None
    if args.split == "evaluation":
        if args.mode != "local" or args.protocol is None:
            raise ValueError("formal sampling requires local mode and a frozen protocol")
        protocol = json.loads(args.protocol.read_text())
        if protocol["identity"] != identity() or protocol["model"] != args.model:
            raise ValueError("protocol runner/corpus/budget/model mismatch")
        if protocol["samples"] != args.samples:
            raise ValueError("sample count differs from frozen protocol")
    cases = DEV if args.split == "dev" else EVALUATION
    args.output.mkdir(parents=True)
    if args.mode == "local":
        preflight = await readiness(args.model)
        write(args.output / "preflight.json", preflight)
        if not preflight["ready"]:
            write(
                args.output / "summary.json",
                {
                    "schema_version": 1,
                    "mode": "local",
                    "complete": False,
                    "strategy_measurement": False,
                    "cost_metrics_valid": False,
                    "expected_runs": len(cases) * args.samples * 3,
                    "actual_runs": 0,
                    "reason": "local model infrastructure unavailable before sampling",
                },
            )
            print("local model preflight failed; no strategy sampling started", flush=True)
            raise SystemExit(2)
    artifacts = model_artifacts(args.model) if args.mode == "local" else None
    if protocol and (
        protocol["model_artifacts"] != artifacts
        or protocol["source_identity"] != snapshot(ROOT)["tree_digest"]
    ):
        raise ValueError("frozen source/model artifacts changed")
    provenance = {
        "identity": identity(),
        "source_identity": snapshot(ROOT)["tree_digest"],
        "mode": args.mode,
        "model": args.model,
        "split": args.split,
        "samples": args.samples,
        "protocol_sha256": digest(protocol) if protocol else None,
        "task_ids": [c.id for c in cases],
        "model_artifacts": artifacts,
        "machine": {
            "system": platform.system(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
        },
    }
    write(args.output / "manifest.json", provenance)
    rows = []
    # Rotate arm order per task/sample to reduce fixed-order warm-cache confounding.
    for index, case in enumerate(cases):
        for sample in range(args.samples):
            offset = (index + sample) % len(ARMS)
            for arm in (*ARMS[offset:], *ARMS[:offset]):
                destination = args.output / case.id / f"{arm}-{sample}"
                row = await run_one(case, arm, sample, args, destination)
                rows.append(row)
                print(
                    f"{len(rows)}/{len(cases) * args.samples * 3} {case.id} {arm} "
                    f"{'pass' if row['success'] else 'fail'} {row['seconds']:.1f}s",
                    flush=True,
                )
    report = summarize(rows, len(cases) * args.samples * 3, args.mode)
    report["source_unchanged"] = snapshot(ROOT)["tree_digest"] == provenance["source_identity"]
    report["strategy_measurement"] = report["strategy_measurement"] and report["source_unchanged"]
    report["manifest_sha256"] = digest(provenance)
    report["model_artifacts_unchanged"] = (
        args.mode == "contract" or model_artifacts(args.model) == artifacts
    )
    report["strategy_measurement"] &= report["model_artifacts_unchanged"]
    write(args.output / "summary.json", report)


def seal(args) -> None:
    dev = json.loads((args.dev_report / "summary.json").read_text())
    manifest = json.loads((args.dev_report / "manifest.json").read_text())
    if not dev["complete"] or not dev["strategy_measurement"] or manifest["identity"] != identity():
        raise ValueError("need a complete live dev baseline matching current runner")
    if args.protocol is None or args.protocol.exists():
        raise ValueError("choose a new protocol path; existing protocols are immutable")
    if not args.protocol.resolve().is_relative_to(ROOT / "reports"):
        raise ValueError("protocol must stay in workspace reports")
    write(
        args.protocol,
        {
            "schema_version": 1,
            "identity": identity(),
            "model": manifest["model"],
            "dev_report_sha256": digest(dev),
            "dev_manifest_sha256": digest(manifest),
            "samples": 3,
            "evaluation_tasks": 30,
            "planned_runs": 270,
            "task_family_counts": dict(Counter(c.family for c in EVALUATION)),
            "decision_rule": {
                "quality_gain": (
                    "paired task-cluster 95% lower bound > 0 against BOTH single and fixed"
                ),
                "latency_exception": (
                    "family paired quality lower bound >= -0.05 and "
                    "p50 latency <= 0.80 of both baselines"
                ),
                "cost_guard": (
                    "multi tokens per successful task <= 1.20 of each baseline, all usage known"
                ),
                "default": "single or fixed when quality gain unclear and cost rises",
                "scope": "exploratory only; 10 tasks/family and no multiplicity correction",
            },
            "source_identity": manifest["source_identity"],
            "model_artifacts": manifest["model_artifacts"],
            "upstream_model_revision_verified": False,
            "corpus": {
                "dev": [asdict(c) for c in DEV],
                "evaluation": [asdict(c) for c in EVALUATION],
            },
            "fee_budget": "existing local service only; no paid API",
            "tool_visibility": {
                "single": "registered material tool schemas; ReAct controls calls",
                "fixed": "deterministic material reads; final model call has no tool schemas",
                "multi": (
                    "deterministic material reads; planner/worker/reviewer have no tool schemas"
                ),
                "authority": "same registered tool and policy; schema omission grants no authority",
            },
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "evaluation"), default="dev")
    parser.add_argument("--mode", choices=("contract", "local"), default="contract")
    parser.add_argument("--model", default="Qwythos-9B-v2-OptiQ-4bit")
    parser.add_argument("--samples", type=int, choices=(1, 3), default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("--dev-report", type=Path)
    args = parser.parse_args()
    if args.seal:
        if args.dev_report is None:
            parser.error("--seal requires --dev-report")
        seal(args)
    else:
        if args.output is None:
            parser.error("--output is required")
        asyncio.run(execute(args))


if __name__ == "__main__":
    main()

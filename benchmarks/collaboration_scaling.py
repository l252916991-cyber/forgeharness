"""When do fixed specialists beat one agent? A scaling study over material count.

Three arms audit the same N independent service configurations (N = 4, 8, 16, 32)
with the same model, rules and answer format:

- ``react``: one ReAct agent that reads each service with ``read_material``;
- ``oneshot``: every service read deterministically, then one model call;
- ``team``: the existing fixed-specialist ``Collaboration``: up to eight specialists,
  N/8 services each, at most two concurrent model calls, then a coordinator.

The expected answer stays in ``evals.multi_material_cases`` and is used only by the
external grader below. Run ``--split dev`` first, ``--seal`` a protocol from it, then
run ``--split evaluation`` once with that protocol. ``--mode contract`` swaps in an
oracle model for wiring tests and never counts as a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from benchmarks.framework_snapshot import snapshot
from evals.multi_material_cases import RULES, SIZES, Case, cases
from forgeharness.evaluation.agent import live_eval_config, public_model_parameters
from forgeharness.models.openai_compatible import OpenAICompatibleModel
from forgeharness.sdk import (
    Actor,
    AgentFinding,
    AgentReport,
    ApplicationSpec,
    AuthorizationRequest,
    Collaboration,
    CollaborationAgent,
    CollaborationEvidence,
    CollaborationPlan,
    CollaborationTask,
    FinalAction,
    Harness,
    Message,
    MessageRole,
    ModelDispatcher,
    ModelRequest,
    ModelResult,
    ModelUsage,
    ParallelExecution,
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
    Usage,
    VerificationRequest,
    VerificationResult,
    verify_trace,
)

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
ARMS = ("react", "oneshot", "team")
MAX_SPECIALISTS = 8
OUTPUT_TOKENS = 1024
ANSWER_FORMAT = (
    "Return only raw JSON, without Markdown code fences: "
    '{"violations": ["svc-01", ...]} listing every violating service ID in ascending '
    "order; use [] if none."
)
SPECIALIST = (
    "You audit service configurations. For each assigned service that currently "
    "violates a rule, add one finding whose claim names the rule (R1, R2 or R3) and "
    "whose evidence_ids cite only that service. Add no findings for compliant services. "
    "If none of your services violate a rule, leave findings empty and put "
    "'no violations among assigned services' in unresolved. Return only raw JSON "
    "without Markdown code fences."
)
COORDINATOR = (
    "You merge specialist audit reports. Add one finding for every service a specialist "
    "reported as violating, citing only that service's evidence ID and naming the rule "
    "in the claim. Add no findings for compliant services. If no service violates a "
    "rule, leave findings empty and put 'no violations' in unresolved. Return only raw "
    "JSON without Markdown code fences."
)
# Published DeepSeek peak list prices in USD per million tokens (deepseek-flash,
# 2026-11). Off-peak is half. An estimate for comparison, not an invoice.
PRICE_PER_MILLION = {"input_miss": 0.30, "input_hit": 0.006, "output": 1.20}
DECISION_RULE = {
    "metric": "exact-set success rate per material count N",
    "best_single": "the higher of react and oneshot success rates; ties go to lower cost",
    "team_worth_it_if": [
        "team success >= best single AND (team p50 latency <= 0.8x OR cost per run <= 0.8x)",
        "team success - best single success >= 0.25",
    ],
    "scope": "exploratory: 4 tasks x 2 samples per arm and N; no multiplicity correction",
}


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def identity() -> dict[str, Any]:
    return {
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(
            (ROOT / "evals/multi_material_cases.py").read_bytes()
        ).hexdigest(),
        "output_tokens": OUTPUT_TOKENS,
        "max_specialists": MAX_SPECIALISTS,
    }


# --------------------------------------------------------------------------- grading

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.S)


def unfence(content: str) -> tuple[str, bool]:
    """Strip one surrounding Markdown fence; applied identically to every arm."""
    match = _FENCE.match(content.strip())
    return (match.group(1), True) if match else (content, False)


def predicted(arm: str, final: str | None) -> list[str] | None:
    """The service IDs an arm claimed; ``None`` when the final answer is malformed."""
    if final is None:
        return None
    try:
        value = json.loads(final)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    if arm == "team":
        try:
            report = AgentReport.model_validate(value)
        except ValueError:
            return None
        return sorted({key for finding in report.findings for key in finding.evidence_ids})
    claimed = value.get("violations")
    if not isinstance(claimed, list) or not all(isinstance(item, str) for item in claimed):
        return None
    return sorted(set(claimed))


def grade(expected: tuple[str, ...], claimed: list[str] | None) -> dict[str, Any]:
    if claimed is None:
        return {"success": False, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    hits = len(set(claimed) & set(expected))
    precision = hits / len(claimed) if claimed else (1.0 if not expected else 0.0)
    recall = hits / len(expected) if expected else 1.0
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "success": tuple(claimed) == tuple(expected),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


# ------------------------------------------------------------------- shared pieces


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str


class Materials:
    input_model = ReadInput
    spec = ToolSpec(
        name="read_material",
        description="Read one service configuration by its service ID.",
        input_schema=ReadInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    def __init__(self, case: Case) -> None:
        self.values = {service.id: service.text for service in case.services}
        self.calls: list[str] = []

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del context
        key = ReadInput.model_validate(arguments).key
        self.calls.append(key)
        return ToolOutput(ok=key in self.values, content=self.values.get(key, "unknown ID"))


class ReadOnly:
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        allowed = request.spec.risk is RiskLevel.READ
        return PolicyDecision(
            type=PolicyDecisionType.ALLOW if allowed else PolicyDecisionType.DENY,
            reason="synthetic read-only study",
        )

    def can_approve(self, **kwargs: object) -> bool:
        return False


class AcceptAll:
    """No correctness feedback inside the loop: every arm is graded only afterwards."""

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        del request
        return VerificationResult(passed=True, reason="graded externally after the run")


class Unfenced:
    """Forward a model, stripping one Markdown fence from final answers, and count it."""

    def __init__(self, inner: Any, counter: list[int]) -> None:
        self._inner, self._counter = inner, counter

    def _clean(self, result: ModelResult) -> ModelResult:
        if isinstance(result.action, FinalAction):
            content, stripped = unfence(result.action.content)
            if stripped:
                self._counter[0] += 1
                return result.model_copy(update={"action": FinalAction(content=content)})
        return result

    async def decide(self, request: ModelRequest) -> ModelResult:
        return self._clean(await self._inner.decide(request))


class OneShot:
    """Read every service deterministically, then make exactly one model call."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    async def run(self, *, kernel: RunKernel, task: str) -> Any:
        services = {}
        for index, key in enumerate(self.keys):
            output = await kernel.call_tool(
                ToolCall(id=f"read-{index}", name="read_material", arguments={"key": key})
            )
            if not output.ok:
                return await kernel.finish(status=RunStatus.FAILED, error="material read failed")
            services[key] = output.content
        result = await kernel.call_model(
            messages=(
                Message(role=MessageRole.SYSTEM, content=RULES + "\n" + ANSWER_FORMAT),
                Message(
                    role=MessageRole.USER,
                    content=json.dumps({"task": task, "services": services}, ensure_ascii=False),
                ),
            ),
            phase="oneshot",
            include_tools=False,
        )
        if not isinstance(result.action, FinalAction):
            return await kernel.finish(status=RunStatus.FAILED, error="tool action returned")
        return await kernel.finish(final=result.action)


def react_budget(size: int) -> RunBudget:
    return RunBudget(
        max_steps=size + 8,
        max_tool_calls=size + 4,
        max_input_tokens=2_000_000,
        max_output_tokens=40_000,
        max_context_tokens=128_000,
        max_wall_seconds=900,
        max_attempts_per_call=1,
    )


def oneshot_budget(size: int) -> RunBudget:
    return RunBudget(
        max_steps=2,
        max_tool_calls=size,
        max_input_tokens=200_000,
        max_output_tokens=8_000,
        max_context_tokens=128_000,
        max_wall_seconds=900,
        max_attempts_per_call=1,
    )


def team_plan(case: Case, model_identity: str) -> CollaborationPlan:
    allowance = Usage(steps=1, input_tokens=16_000, output_tokens=OUTPUT_TOKENS)
    groups = min(MAX_SPECIALISTS, case.size)
    ids = [service.id for service in case.services]
    share = math.ceil(len(ids) / groups)
    return CollaborationPlan(
        version=f"scaling-{case.id}",
        model_identity=model_identity,
        goal=RULES,
        agents=(
            CollaborationAgent(
                id="auditor", version="1", instructions=SPECIALIST, max_usage=allowance
            ),
            CollaborationAgent(
                id="coordinator", version="1", instructions=COORDINATOR, max_usage=allowance
            ),
        ),
        evidence=tuple(
            CollaborationEvidence(
                id=service.id,
                resource_id=f"{case.id}/{service.id}",
                revision="1",
                text=service.text,
            )
            for service in case.services
        ),
        tasks=tuple(
            CollaborationTask(
                id=f"group-{number + 1}",
                agent_id="auditor",
                objective="Audit the assigned services against the rules.",
                evidence_ids=tuple(ids[number * share : (number + 1) * share]),
            )
            for number in range(groups)
            if ids[number * share : (number + 1) * share]
        ),
        coordinator_id="coordinator",
    )


def team_budget(plan: CollaborationPlan) -> RunBudget:
    nodes = len(plan.tasks) + 1
    return RunBudget(
        max_steps=nodes,
        max_tool_calls=0,
        max_input_tokens=16_000 * nodes,
        max_output_tokens=OUTPUT_TOKENS * nodes,
        max_context_tokens=64_000,
        max_wall_seconds=900,
    )


# --------------------------------------------------------------------- oracle model


class ContractModel:
    """Wiring oracle that answers correctly; never qualifies as a measurement."""

    def __init__(self, case: Case) -> None:
        self.case = case
        self.flagged = set(case.violations)

    async def decide(self, request: ModelRequest) -> ModelResult:
        usage = ModelUsage(input_tokens=10, output_tokens=5)
        for message in request.messages:
            if message.role is not MessageRole.USER:
                continue
            try:
                value = json.loads(message.content or "")
            except ValueError:
                continue
            if isinstance(value, dict) and "collaboration_assignment" in value:
                task_id = value["collaboration_assignment"]
                assigned = (
                    sorted(self.flagged)
                    if task_id == "synthesis"
                    else [e["id"] for e in value["evidence"] if e["id"] in self.flagged]
                )
                report = AgentReport(
                    task_id=task_id,
                    summary="oracle",
                    findings=tuple(
                        AgentFinding(claim="violation", evidence_ids=(key,)) for key in assigned
                    ),
                    unresolved=() if assigned else ("no violations among assigned services",),
                )
                content = "```json\n" + report.model_dump_json() + "\n```"
                return ModelResult(action=FinalAction(content=content), usage=usage)
        if request.tools and not any(m.role is MessageRole.TOOL for m in request.messages):
            call = ToolCall(id="r0", name="read_material", arguments={"key": "svc-01"})
            return ModelResult(action=ToolAction(call=call), usage=usage)
        answer = json.dumps({"violations": sorted(self.flagged)})
        return ModelResult(action=FinalAction(content=answer), usage=usage)


# ----------------------------------------------------------------------- one run


class Meter:
    """Provider-reported usage per HTTP response, identical for every arm."""

    def __init__(self) -> None:
        self.requests = 0
        self.responses: list[dict[str, Any]] = []

    async def on_request(self, request: httpx.Request) -> None:
        del request
        self.requests += 1

    async def on_response(self, response: httpx.Response) -> None:
        await response.aread()
        entry: dict[str, Any] = {"status": response.status_code, "complete": False}
        try:
            usage = response.json().get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            entry.update(
                prompt=int(usage["prompt_tokens"]),
                completion=int(usage["completion_tokens"]),
                cached=int(usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0))),
                complete=True,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        self.responses.append(entry)


def context_for(application_id: str, version: str, policy: str, run_id: str) -> RunContext:
    return RunContext(
        application_id=application_id,
        application_version=version,
        policy_version=policy,
        run_id=run_id,
        session_id="scaling-study",
        correlation_id="scaling-study",
        actor=Actor(id="scaling-study"),
        scope=ResourceScope(tenant_id="synthetic"),
    )


async def run_one(case: Case, arm: str, sample: int, args: Any, destination: Path) -> dict:
    meter = Meter()
    fences = [0]
    client = httpx.AsyncClient(
        timeout=120, event_hooks={"request": [meter.on_request], "response": [meter.on_response]}
    )
    config = live_eval_config(
        base_url=args.base_url,
        api_key=os.environ.get("FORGE_API_KEY", "contract-no-key"),
        model=args.model,
        max_tokens=OUTPUT_TOKENS,
    )

    def factory() -> Any:
        inner = (
            ContractModel(case)
            if args.mode == "contract"
            else OpenAICompatibleModel(config, client=client)
        )
        return Unfenced(inner, fences)

    state = destination / "state"
    state.mkdir(parents=True, mode=0o700)
    tool = Materials(case)
    run_id = f"{case.id}-{arm}-{sample}"
    task = "Service IDs: " + ", ".join(tool.values)
    started = time.monotonic()
    final: str | None = None
    status, error, trace_valid = "error", None, False
    try:
        if arm == "team":
            plan = team_plan(case, f"{args.model}:t0:max{OUTPUT_TOKENS}")
            context = context_for(
                "collaboration", plan.version, "collaboration-readonly-v1", run_id
            )
            # Parallel R2 needs a provider capacity domain shared by all its nodes.
            with ModelDispatcher(
                state_dir=destination / "capacity", provider_id="study", capacity=2
            ) as dispatcher:
                team = Collaboration(
                    plan=plan,
                    model_factory=factory,
                    budget=team_budget(plan),
                    state_dir=state,
                    authorize_run=lambda ctx: True,
                    validate_resources=lambda ctx, revisions: True,
                    execution=ParallelExecution(max_in_flight=2, model_concurrency_safe=True),
                    dispatcher=dispatcher,
                )
                result = await team.harness.run(context=context, task=plan.goal)
            trace = team.harness.trace_path(context)
        else:
            spec = ApplicationSpec(
                id="scaling-study",
                version="1",
                policy_version="1",
                model_factory=factory,
                tools=(tool,),
                policy=ReadOnly(),
                verifier=AcceptAll(),
                budget=react_budget(case.size) if arm == "react" else oneshot_budget(case.size),
                instructions=(RULES + "\n" + ANSWER_FORMAT,),
            )
            harness = Harness(
                spec,
                state_dir=state,
                orchestrator=OneShot(list(tool.values)) if arm == "oneshot" else None,
            )
            context = context_for(spec.id, "1", "1", run_id)
            result = await harness.run(
                context=context, task=task + "\nRead each service with read_material."
            )
            trace = harness.trace_path(context)
        status, error, final = result.status.value, result.error, result.final_output
        trace_valid = verify_trace(trace).valid
    except Exception as exc:
        # A bounded exception class only: provider bodies and credentials stay out.
        error = type(exc).__name__
    finally:
        await client.aclose()
    seconds = time.monotonic() - started
    claimed = predicted(arm, final) if status == "succeeded" else None
    measured = [r for r in meter.responses if r["complete"]]
    prompt = sum(r["prompt"] for r in measured)
    cached = sum(r["cached"] for r in measured)
    completion = sum(r["completion"] for r in measured)
    row = {
        "case": case.id,
        "size": case.size,
        "arm": arm,
        "sample": sample,
        "status": status,
        "error": error,
        "expected": list(case.violations),
        "claimed": claimed,
        **grade(case.violations, claimed),
        "trace_valid": trace_valid,
        "seconds": seconds,
        "model_calls": meter.requests,
        "prompt_tokens": prompt,
        "cached_prompt_tokens": cached,
        "completion_tokens": completion,
        "max_prompt_tokens": max((r["prompt"] for r in measured), default=0),
        "cost_usd_peak": (
            (prompt - cached) * PRICE_PER_MILLION["input_miss"]
            + cached * PRICE_PER_MILLION["input_hit"]
            + completion * PRICE_PER_MILLION["output"]
        )
        / 1_000_000,
        "fences_stripped": fences[0],
        "material_reads": len(tool.calls),
        "usage_known": args.mode == "live" and len(measured) == meter.requests > 0,
        "infrastructure_error": args.mode == "live"
        and (
            len(meter.responses) < meter.requests
            or any(r["status"] >= 400 for r in meter.responses)
        ),
    }
    row["success"] = bool(row["success"] and trace_valid)
    write(destination / "result.json", row)
    return row


# ---------------------------------------------------------------------- summary


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)]


def summarize(rows: list[dict], expected_runs: int, mode: str) -> dict[str, Any]:
    by_size: dict[str, Any] = {}
    for size in SIZES:
        arms = {}
        for arm in ARMS:
            selected = [r for r in rows if r["size"] == size and r["arm"] == arm]
            if not selected:
                continue
            successes = sum(r["success"] for r in selected)
            cost = sum(r["cost_usd_peak"] for r in selected)
            arms[arm] = {
                "runs": len(selected),
                "successes": successes,
                "success_rate": successes / len(selected),
                "mean_f1": statistics.mean(r["f1"] for r in selected),
                "latency_p50_seconds": percentile([r["seconds"] for r in selected], 0.5),
                "latency_p95_seconds": percentile([r["seconds"] for r in selected], 0.95),
                "mean_model_calls": statistics.mean(r["model_calls"] for r in selected),
                "mean_prompt_tokens": statistics.mean(r["prompt_tokens"] for r in selected),
                "mean_max_prompt_tokens": statistics.mean(r["max_prompt_tokens"] for r in selected),
                "cost_usd_peak_per_run": cost / len(selected),
                "cost_usd_peak_per_success": cost / successes if successes else None,
                "fences_stripped": sum(r["fences_stripped"] for r in selected),
                "malformed_or_failed_runs": sum(r["claimed"] is None for r in selected),
            }
        by_size[str(size)] = {"arms": arms, "decision": decide(arms)}
    infra = sum(r["infrastructure_error"] for r in rows)
    complete = (
        len(rows) == expected_runs
        and len({(r["case"], r["arm"], r["sample"]) for r in rows}) == expected_runs
    )
    return {
        "schema_version": 1,
        "mode": mode,
        "expected_runs": expected_runs,
        "actual_runs": len(rows),
        "complete": complete,
        "infrastructure_failures": infra,
        "measurement": mode == "live"
        and complete
        and infra == 0
        and all(r["trace_valid"] for r in rows)
        and all(r["usage_known"] for r in rows),
        "decision_rule": DECISION_RULE,
        "by_size": by_size,
        "price_basis": {"usd_per_million_tokens_peak": PRICE_PER_MILLION},
        "limits": [
            "synthetic author-generated audit tasks; not business data",
            "one model; at most two concurrent model calls in the team arm",
            "4 tasks x 2 samples per arm and size; exploratory",
        ],
    }


def decide(arms: dict[str, Any]) -> str:
    if not {"react", "oneshot", "team"} <= set(arms):
        return "incomplete"
    singles = sorted(
        (arms["react"], arms["oneshot"]),
        key=lambda a: (-a["success_rate"], a["cost_usd_peak_per_run"]),
    )
    best, team = singles[0], arms["team"]
    if team["success_rate"] - best["success_rate"] >= 0.25:
        return "team_worth_it:quality"
    if team["success_rate"] >= best["success_rate"] and (
        (team["latency_p50_seconds"] or 0) <= 0.8 * (best["latency_p50_seconds"] or 0)
        or team["cost_usd_peak_per_run"] <= 0.8 * best["cost_usd_peak_per_run"]
    ):
        return "team_worth_it:efficiency"
    return "team_not_worth_it"


# ------------------------------------------------------------------------ driver


async def execute(args: Any) -> dict[str, Any]:
    if not args.output.resolve().is_relative_to(REPORTS.resolve()):
        raise ValueError("output must be inside workspace reports")
    if args.output.exists():
        raise ValueError("output already exists; keep attempts and choose a new directory")
    if args.mode == "live" and not os.environ.get("FORGE_API_KEY"):
        raise ValueError("live mode needs FORGE_API_KEY in the environment")
    protocol = None
    if args.split == "evaluation" and args.mode == "live":
        if args.protocol is None:
            raise ValueError("the evaluation split needs a sealed protocol")
        protocol = json.loads(args.protocol.read_text())
        if protocol["identity"] != identity() or protocol["model"] != args.model:
            raise ValueError("protocol runner, corpus or model mismatch")
        if protocol["samples"] != args.samples:
            raise ValueError("sample count differs from the sealed protocol")
    selected = cases(args.split)
    if args.sizes:
        selected = tuple(c for c in selected if c.size in args.sizes)
    expected_runs = len(selected) * len(ARMS) * args.samples
    args.output.mkdir(parents=True)
    manifest = {
        "identity": identity(),
        "source_identity": snapshot(ROOT)["tree_digest"],
        "mode": args.mode,
        "model": args.model,
        "model_parameters": public_model_parameters(
            live_eval_config(
                base_url=args.base_url,
                api_key="omitted",
                model=args.model,
                max_tokens=OUTPUT_TOKENS,
            )
        ),
        "split": args.split,
        "samples": args.samples,
        "protocol_sha256": digest(protocol) if protocol else None,
        "cases": [c.id for c in selected],
    }
    write(args.output / "manifest.json", manifest)
    rows = []
    for index, case in enumerate(selected):
        for sample in range(args.samples):
            # Rotate arm order so no arm always runs first against a warm cache.
            offset = (index + sample) % len(ARMS)
            for arm in (*ARMS[offset:], *ARMS[:offset]):
                row = await run_one(
                    case, arm, sample, args, args.output / case.id / f"{arm}-{sample}"
                )
                rows.append(row)
                print(
                    f"{len(rows)}/{expected_runs} {case.id} {arm:<7} "
                    f"{'pass' if row['success'] else 'fail'} f1={row['f1']:.2f} "
                    f"{row['seconds']:.1f}s calls={row['model_calls']}",
                    flush=True,
                )
    summary = summarize(rows, expected_runs, args.mode)
    summary["source_unchanged"] = snapshot(ROOT)["tree_digest"] == manifest["source_identity"]
    summary["measurement"] = summary["measurement"] and summary["source_unchanged"]
    summary["manifest_sha256"] = digest(manifest)
    write(args.output / "summary.json", summary)
    for size, entry in summary["by_size"].items():
        rates = {arm: f"{a['successes']}/{a['runs']}" for arm, a in entry["arms"].items()}
        print(f"N={size}: {rates} -> {entry['decision']}", flush=True)
    return summary


def seal(args: Any) -> None:
    summary = json.loads((args.dev_report / "summary.json").read_text())
    manifest = json.loads((args.dev_report / "manifest.json").read_text())
    if not summary["complete"] or manifest["mode"] != "live":
        raise ValueError("need a complete live dev run")
    if manifest["identity"] != identity():
        raise ValueError("the dev run used a different runner or corpus")
    if args.protocol is None or args.protocol.exists():
        raise ValueError("choose a new protocol path; protocols are immutable")
    write(
        args.protocol,
        {
            "schema_version": 1,
            "identity": identity(),
            "model": manifest["model"],
            "samples": 2,
            "evaluation_cases": [c.id for c in cases("evaluation")],
            "planned_runs": len(cases("evaluation")) * len(ARMS) * 2,
            "dev_summary_sha256": digest(summary),
            "decision_rule": DECISION_RULE,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "evaluation"), default="dev")
    parser.add_argument("--mode", choices=("contract", "live"), default="contract")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--samples", type=int, choices=(1, 2), default=1)
    parser.add_argument("--sizes", type=int, nargs="*", choices=SIZES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("--dev-report", type=Path)
    args = parser.parse_args()
    if args.seal:
        if args.dev_report is None:
            parser.error("--seal requires --dev-report")
        seal(args)
        return
    if args.output is None:
        parser.error("--output is required")
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()

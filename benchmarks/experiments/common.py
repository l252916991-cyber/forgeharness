"""Shared machinery for the collaboration experiments (scaling, ablation, long context).

Everything here is identical across studies: the audit-task tools and grading, the
model wrappers, provider metering, the provider-side rerun rule, sealing and the
command line. Each study module keeps only its arms, its pre-registered rule and its
summary. Run a study with ``python -m benchmarks.experiments.<study>``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from benchmarks.experiments.provider_guard import AccountRefused, ProviderGuard
from evals.audit_tasks import RULES, Case
from forgeharness.evaluation.agent import live_eval_config, public_model_parameters
from forgeharness.models.openai_compatible import OpenAICompatibleModel
from forgeharness.sdk import (
    Actor,
    AgentFinding,
    AgentReport,
    AuthorizationRequest,
    CollaborationAgent,
    CollaborationEvidence,
    CollaborationPlan,
    CollaborationTask,
    FinalAction,
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
    Usage,
    VerificationRequest,
    VerificationResult,
)

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports"
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
INFRA_ATTEMPTS = 3
INFRA_BACKOFF_SECONDS = (10.0, 30.0)
STALL_SECONDS = 30.0
INFRASTRUCTURE_RULE = (
    "an attempt that saw HTTP 429/5xx or a request without a response is rerun from "
    "fresh state, at most 3 attempts; only the last attempt is graded; every attempt is "
    "kept. HTTP 401/402 stops the whole run"
)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def file_sha256(*paths: str) -> dict[str, str]:
    """Digests of repository files, keyed by path, for a study's sealed identity."""
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def cost(prompt: int, cached: int, completion: int) -> float:
    """Peak list price in USD for one run's provider-reported tokens."""
    return (
        (prompt - cached) * PRICE_PER_MILLION["input_miss"]
        + cached * PRICE_PER_MILLION["input_hit"]
        + completion * PRICE_PER_MILLION["output"]
    ) / 1_000_000


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)]


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
    if arm.startswith("team"):
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
    """Exact-set success plus item-level scores; compared as sets, so ID order and
    width (``svc-99`` against ``svc-100``) never matter."""
    if claimed is None:
        return {"success": False, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    hits = len(set(claimed) & set(expected))
    precision = hits / len(claimed) if claimed else (1.0 if not expected else 0.0)
    recall = hits / len(expected) if expected else 1.0
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "success": set(claimed) == set(expected),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


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


class Meter:
    """Provider-reported usage per HTTP response, identical for every arm."""

    def __init__(self) -> None:
        self.requests = 0
        self.responses: list[dict[str, Any]] = []
        self._started: dict[int, float] = {}

    async def on_request(self, request: httpx.Request) -> None:
        self.requests += 1
        self._started[id(request)] = time.monotonic()

    async def on_response(self, response: httpx.Response) -> None:
        started = self._started.pop(id(response.request), None)
        headers = time.monotonic()
        await response.aread()
        done = time.monotonic()
        entry: dict[str, Any] = {
            "status": response.status_code,
            "complete": False,
            # Headers versus body separates a slow provider answer from a queue that
            # returns headers at once and holds the body.
            "headers_seconds": None if started is None else round(headers - started, 3),
            "seconds": None if started is None else round(done - started, 3),
        }
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


# ------------------------------------------------------------------ plans, budgets


def team_plan(
    case: Case,
    model_identity: str,
    *,
    specialists: int,
    output_tokens: int,
    instructions: str = SPECIALIST,
    version_prefix: str = "scaling",
) -> CollaborationPlan:
    """Fixed specialists over contiguous slices of ``case``, then a coordinator."""
    allowance = Usage(steps=1, input_tokens=16_000, output_tokens=output_tokens)
    groups = min(specialists, case.size)
    ids = [service.id for service in case.services]
    share = math.ceil(len(ids) / groups)
    return CollaborationPlan(
        version=f"{version_prefix}-{case.id}",
        model_identity=model_identity,
        goal=RULES,
        agents=(
            CollaborationAgent(
                id="auditor", version="1", instructions=instructions, max_usage=allowance
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


def team_budget(plan: CollaborationPlan, output_tokens: int) -> RunBudget:
    nodes = len(plan.tasks) + 1
    return RunBudget(
        max_steps=nodes,
        max_tool_calls=0,
        max_input_tokens=16_000 * nodes,
        max_output_tokens=output_tokens * nodes,
        max_context_tokens=64_000,
        max_wall_seconds=900,
    )


def oneshot_budget(
    size: int, output_tokens: int, *, max_context_tokens: int = 128_000, wall_seconds: int = 900
) -> RunBudget:
    return RunBudget(
        max_steps=2,
        max_tool_calls=size,
        max_input_tokens=max(200_000, max_context_tokens),
        max_output_tokens=output_tokens * 2,
        max_context_tokens=max_context_tokens,
        max_wall_seconds=wall_seconds,
        max_attempts_per_call=1,
    )


async def run_team(
    plan: CollaborationPlan,
    factory: Callable[[], Any],
    destination: Path,
    run_id: str,
    output_tokens: int,
) -> tuple[str, str | None, str | None, Path]:
    """One fixed-specialist run: two model calls in flight, coordinator last."""
    from forgeharness.sdk import Collaboration, ModelDispatcher, ParallelExecution

    state = destination / "state"
    state.mkdir(parents=True, mode=0o700)
    context = context_for("collaboration", plan.version, "collaboration-readonly-v1", run_id)
    # Parallel R2 needs a provider capacity domain shared by all its nodes.
    with ModelDispatcher(
        state_dir=destination / "capacity", provider_id="study", capacity=2
    ) as dispatcher:
        team = Collaboration(
            plan=plan,
            model_factory=factory,
            budget=team_budget(plan, output_tokens),
            state_dir=state,
            authorize_run=lambda ctx: True,
            validate_resources=lambda ctx, revisions: True,
            execution=ParallelExecution(max_in_flight=2, model_concurrency_safe=True),
            dispatcher=dispatcher,
        )
        result = await team.harness.run(context=context, task=plan.goal)
    return result.status.value, result.error, result.final_output, team.harness.trace_path(context)


# ------------------------------------------------------------- provider metering


class Provider:
    """One unit's HTTP client: metered, guarded, and a model factory for any case.

    ``guard`` is shared by the whole run so one account refusal stops every later unit.
    """

    def __init__(self, args: Any, guard: ProviderGuard, output_tokens: int, timeout: float):
        self.args, self.guard, self.meter, self.fences = args, guard, Meter(), [0]
        self.client = httpx.AsyncClient(
            timeout=timeout,
            event_hooks={
                "request": [self.meter.on_request],
                "response": [self.meter.on_response, guard.on_response],
            },
        )
        self.config = live_eval_config(
            base_url=args.base_url,
            api_key=os.environ.get("FORGE_API_KEY", "contract-no-key"),
            model=args.model,
            max_tokens=output_tokens,
        )

    def inner(self, case: Case) -> Any:
        """The provider model, or the oracle in contract mode."""
        if self.args.mode == "contract":
            return ContractModel(case)
        return OpenAICompatibleModel(self.config, client=self.client)

    def model(self, case: Case) -> Unfenced:
        return Unfenced(self.inner(case), self.fences)

    async def close(self) -> None:
        await self.client.aclose()

    def metrics(self) -> dict[str, Any]:
        """Provider usage and infrastructure facts for one attempt, identical in every study."""
        meter, live = self.meter, self.args.mode == "live"
        measured = [r for r in meter.responses if r["complete"]]
        statuses = [r["status"] for r in meter.responses]
        missing = meter.requests - len(meter.responses)
        calls = [r["seconds"] for r in meter.responses if r["seconds"] is not None]
        prompt = sum(r["prompt"] for r in measured)
        cached = sum(r["cached"] for r in measured)
        completion = sum(r["completion"] for r in measured)
        return {
            "model_calls": meter.requests,
            "prompt_tokens": prompt,
            "cached_prompt_tokens": cached,
            "completion_tokens": completion,
            "max_prompt_tokens": max((r["prompt"] for r in measured), default=0),
            "cost_usd_peak": cost(prompt, cached, completion),
            "fences_stripped": self.fences[0],
            "usage_known": live and len(measured) == meter.requests > 0,
            "http_statuses": statuses,
            "missing_responses": missing,
            "call_seconds": calls,
            "max_call_seconds": max(calls, default=None),
            "stalled_calls": sum(seconds > STALL_SECONDS for seconds in calls),
            "infrastructure_error": live
            and (missing > 0 or any(status >= 400 for status in statuses)),
            # Only provider-side trouble is rerun; a client error would recur every time.
            "infrastructure_retryable": live
            and (missing > 0 or any(status == 429 or status >= 500 for status in statuses))
            and not any(400 <= status < 500 and status != 429 for status in statuses),
        }


async def measured(
    attempt: Callable[[Path], Awaitable[dict[str, Any]]], destination: Path
) -> dict[str, Any]:
    """Run one graded unit, rerunning provider-side failures from fresh state.

    Each attempt keeps its own directory; the returned row is the last attempt, written
    as ``result.json`` beside them with the earlier attempts listed.
    """
    discarded: list[dict[str, Any]] = []
    for number in range(INFRA_ATTEMPTS):
        row = await attempt(destination / f"attempt-{number}")
        if not row["infrastructure_retryable"] or number == INFRA_ATTEMPTS - 1:
            break
        discarded.append(
            {
                key: row.get(key)
                for key in (
                    "error",
                    "http_statuses",
                    "missing_responses",
                    "seconds",
                    "cost_usd_peak",
                    "prompt_tokens",
                    "completion_tokens",
                )
            }
            | {"attempt": number}
        )
        await asyncio.sleep(INFRA_BACKOFF_SECONDS[min(number, len(INFRA_BACKOFF_SECONDS) - 1)])
    row = {**row, "attempt": number, "discarded_attempts": discarded}
    write(destination / "result.json", row)
    return row


def timed() -> Callable[[], float]:
    started = time.monotonic()
    return lambda: time.monotonic() - started


# ------------------------------------------------------------------------ driver


class Study:
    """What the shared driver needs from a study module."""

    def __init__(
        self,
        *,
        name: str,
        identity: Callable[[], dict[str, Any]],
        cases: Callable[[str], tuple[Case, ...]],
        evaluation_split: str,
        output_tokens: int,
    ) -> None:
        self.name, self.identity, self.cases = name, identity, cases
        self.evaluation_split, self.output_tokens = evaluation_split, output_tokens


def start(study: Study, args: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Refuse unsafe outputs and unsealed evaluations, then write the run manifest."""
    if not args.output.resolve().is_relative_to(REPORTS.resolve()):
        raise ValueError("output must be inside workspace reports")
    if args.output.exists():
        raise ValueError("output already exists; keep attempts and choose a new directory")
    if args.mode == "live" and not os.environ.get("FORGE_API_KEY"):
        raise ValueError("live mode needs FORGE_API_KEY in the environment")
    protocol = None
    if args.split == study.evaluation_split and args.mode == "live":
        if args.protocol is None:
            raise ValueError(f"the {study.evaluation_split} split needs a sealed protocol")
        protocol = json.loads(args.protocol.read_text())
        if protocol["identity"] != study.identity() or protocol["model"] != args.model:
            raise ValueError("protocol runner, corpus or model mismatch")
    args.output.mkdir(parents=True)
    from benchmarks.framework_snapshot import snapshot

    manifest = {
        "identity": study.identity(),
        "source_identity": snapshot(ROOT)["tree_digest"],
        "mode": args.mode,
        "model": args.model,
        "model_parameters": public_model_parameters(
            live_eval_config(
                base_url=args.base_url,
                api_key="omitted",
                model=args.model,
                max_tokens=study.output_tokens,
            )
        ),
        "split": args.split,
        "protocol_sha256": digest(protocol) if protocol else None,
    }
    return manifest, protocol


def finish(
    args: Any, manifest: dict[str, Any], summary: dict[str, Any], guard: ProviderGuard
) -> dict[str, Any]:
    """Stamp source stability and any early stop on the summary, then write it."""
    from benchmarks.framework_snapshot import snapshot

    summary.update(guard.summary())
    summary["source_unchanged"] = snapshot(ROOT)["tree_digest"] == manifest["source_identity"]
    summary["measurement"] = (
        summary["measurement"] and summary["source_unchanged"] and not summary["stopped_early"]
    )
    summary["manifest_sha256"] = digest(manifest)
    write(args.output / "summary.json", summary)
    return summary


def seal(
    study: Study, args: Any, extra: dict[str, Any], checks: Callable[[dict[str, Any]], None]
) -> None:
    """Write an immutable protocol from a clean, complete live dev run.

    The gate is pipeline health, not results: every arm must have reached the provider
    cleanly and produced parseable answers before the evaluation budget is spent.
    """
    summary = json.loads((args.dev_report / "summary.json").read_text())
    manifest = json.loads((args.dev_report / "manifest.json").read_text())
    if not summary["complete"] or manifest["mode"] != "live" or manifest["split"] != "dev":
        raise ValueError("need a complete live dev run")
    if manifest["identity"] != study.identity():
        raise ValueError("the dev run used a different runner or corpus")
    if not summary["measurement"]:
        raise ValueError("the dev run is not a clean measurement; inspect it before sealing")
    checks(summary)
    if args.protocol is None or args.protocol.exists():
        raise ValueError("choose a new protocol path; protocols are immutable")
    write(
        args.protocol,
        {
            "schema_version": 1,
            "identity": study.identity(),
            "model": manifest["model"],
            "dev_summary_sha256": digest(summary),
            **extra,
        },
    )


def cli(
    study: Study,
    description: str,
    execute: Callable[[Any], Coroutine[Any, Any, dict[str, Any]]],
    seal_run: Callable[[Any], None],
    configure: Callable[[argparse.ArgumentParser], None] | None = None,
) -> None:
    """The command line every study shares; refusals exit 2 so chained commands stop."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--split", choices=("dev", study.evaluation_split), default="dev")
    parser.add_argument("--mode", choices=("contract", "live"), default="contract")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("--dev-report", type=Path)
    if configure is not None:
        configure(parser)
    args = parser.parse_args()
    if args.seal:
        if args.dev_report is None:
            parser.error("--seal requires --dev-report")
        try:
            seal_run(args)
        except ValueError as exc:
            parser.exit(2, f"seal refused: {exc}\n")
        print(f"sealed {args.protocol}", flush=True)
        return
    if args.output is None:
        parser.error("--output is required")
    try:
        asyncio.run(execute(args))
    except (ValueError, AccountRefused) as exc:
        parser.exit(2, f"run refused: {exc}\n")

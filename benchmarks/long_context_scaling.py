"""Where should collaboration win? Single call versus fan-out as material grows to 420k tokens.

The scaling and ablation studies found one plain call over every material perfect
up to N = 32 (about 13k tokens). This study grows N to 128, 512 and 1024 (about
53k, 211k and 422k tokens) on the same audit task and asks whether the single call
degrades, whether fanning out restores accuracy, and whether the fan-out needs the
collaboration framework. The plan, including the rule below, is
``reports/long-context-scaling/PLAN.md``.

Arms, all with the same model, rules, answer format and output limit:

- ``oneshot``: every material read deterministically, then one model call;
- ``split-32``: groups of 32 materials, each exactly the oneshot arm (perfect at
  N = 32 in both earlier studies), at most 8 groups in flight, answers united;
- ``team-16``: groups of 16 materials, each a ``Collaboration`` of 8 specialists x 2
  materials and a coordinator (the ablation's best team), at most 4 plans in flight
  (so at most 8 model calls), answers united. A plan holds at most 32 materials, so
  scaling the framework past that needs this program-level grouping anyway.

Each unit (the single call, a group, a plan) runs in its own state directory with
its own HTTP client and meter, so a provider-side failure reruns only that unit.
Run ``--split dev``, ``--seal`` a protocol from it, then ``--split evaluation`` once.
``--mode contract`` swaps in the scaling study's oracle and never counts as a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from benchmarks.collaboration_scaling import (
    ANSWER_FORMAT,
    COORDINATOR,
    INFRA_ATTEMPTS,
    INFRA_BACKOFF_SECONDS,
    PRICE_PER_MILLION,
    SPECIALIST,
    STALL_SECONDS,
    AcceptAll,
    ContractModel,
    Materials,
    Meter,
    OneShot,
    ReadOnly,
    Unfenced,
    context_for,
    digest,
    percentile,
    predicted,
    write,
)
from benchmarks.framework_snapshot import snapshot
from evals.long_context_cases import SIZES, long_cases
from evals.multi_material_cases import RULES, Case
from forgeharness.evaluation.agent import live_eval_config, public_model_parameters
from forgeharness.models.openai_compatible import OpenAICompatibleModel
from forgeharness.sdk import (
    ApplicationSpec,
    Collaboration,
    CollaborationAgent,
    CollaborationEvidence,
    CollaborationPlan,
    CollaborationTask,
    Harness,
    ModelDispatcher,
    ParallelExecution,
    RunBudget,
    Usage,
    verify_trace,
)

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
ARMS = ("oneshot", "split-32", "team-16")
OUTPUT_TOKENS = 4096
SPLIT_GROUP = 32
TEAM_GROUP = 16
TEAM_SPECIALISTS = 8
SPLIT_IN_FLIGHT = 8
TEAM_PLANS_IN_FLIGHT = 4
HTTP_TIMEOUT_SECONDS = 600.0
RULE = {
    "primary": "service-level error rate: (misses + false alarms) / (N x tasks), per arm and N",
    "secondary": "exact-set success per run; cost and latency are descriptive only",
    "failed_units": (
        "a unit (the single call, a group or a plan) without a parseable answer claims "
        "nothing: its violators count as misses, and failed units are reported"
    ),
    "oneshot_degrades": "at N > 32: oneshot errors >= 10 and error rate >= 0.5%",
    "fanout_worth_it": (
        "at N: oneshot errors >= 10 and the fan-out arm's errors are at most half of oneshot's"
    ),
    "framework_vs_program": (
        "at N: framework_better if split-32 errors >= 10 and team-16 errors are at most half "
        "of split-32's; program_better if team-16 errors >= 10 and split-32 errors are at "
        "most half of team-16's; otherwise no_clear_difference"
    ),
    "infrastructure": (
        "a unit attempt that saw HTTP 429/5xx or a request without a response is rerun from "
        "fresh state, at most 3 attempts per unit; only the last attempt is graded; every "
        "attempt is kept"
    ),
    "scope": "exploratory: one model, temperature 0, 4 tasks per N, one sample each",
}


def identity() -> dict[str, Any]:
    def sha(path: str) -> str:
        return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()

    return {
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "shared_runner_sha256": sha("benchmarks/collaboration_scaling.py"),
        "cases_sha256": sha("evals/multi_material_cases.py"),
        "long_cases_sha256": sha("evals/long_context_cases.py"),
        "output_tokens": OUTPUT_TOKENS,
        "groups": {"split": SPLIT_GROUP, "team": TEAM_GROUP},
        "in_flight": {"split": SPLIT_IN_FLIGHT, "team_plans": TEAM_PLANS_IN_FLIGHT},
    }


# ---------------------------------------------------------------------- units


def groups(case: Case, size: int) -> list[Case]:
    """Contiguous sub-cases of at most ``size`` materials; service IDs are unchanged."""
    return [
        Case(
            id=f"{case.id}-g{number + 1:02d}",
            split=case.split,
            size=len(case.services[start : start + size]),
            services=case.services[start : start + size],
        )
        for number, start in enumerate(range(0, case.size, size))
    ]


def team_plan(case: Case, model_identity: str) -> CollaborationPlan:
    """The ablation's best team for one group: 8 specialists x 2 materials, a coordinator."""
    allowance = Usage(steps=1, input_tokens=16_000, output_tokens=OUTPUT_TOKENS)
    specialists = min(TEAM_SPECIALISTS, case.size)
    ids = [service.id for service in case.services]
    share = math.ceil(len(ids) / specialists)
    return CollaborationPlan(
        version=f"long-{case.id}",
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
            for number in range(specialists)
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


def oneshot_budget(size: int) -> RunBudget:
    return RunBudget(
        max_steps=2,
        max_tool_calls=size,
        max_input_tokens=1_000_000,
        max_output_tokens=OUTPUT_TOKENS * 2,
        max_context_tokens=900_000,
        max_wall_seconds=1_800,
        max_attempts_per_call=1,
    )


async def run_unit(unit: Case, kind: str, args: Any, destination: Path) -> dict[str, Any]:
    """One oneshot call or one collaboration plan over ``unit``, metered on its own."""
    meter = Meter()
    fences = [0]
    client = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        event_hooks={"request": [meter.on_request], "response": [meter.on_response]},
    )
    config = live_eval_config(
        base_url=args.base_url,
        api_key=os.environ.get("FORGE_API_KEY", "contract-no-key"),
        model=args.model,
        max_tokens=OUTPUT_TOKENS,
    )

    def factory() -> Any:
        inner = (
            ContractModel(unit)
            if args.mode == "contract"
            else OpenAICompatibleModel(config, client=client)
        )
        return Unfenced(inner, fences)

    state = destination / "state"
    state.mkdir(parents=True, mode=0o700)
    run_id = f"{unit.id}-{kind}"
    started = time.monotonic()
    status, error, claimed, trace_valid = "error", None, None, False
    tool = Materials(unit)
    try:
        if kind == "team":
            plan = team_plan(unit, f"{args.model}:t0:max{OUTPUT_TOKENS}")
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
                budget=oneshot_budget(unit.size),
                instructions=(RULES + "\n" + ANSWER_FORMAT,),
            )
            harness = Harness(spec, state_dir=state, orchestrator=OneShot(list(tool.values)))
            context = context_for(spec.id, "1", "1", run_id)
            task = "Service IDs: " + ", ".join(tool.values)
            result = await harness.run(
                context=context, task=task + "\nRead each service with read_material."
            )
            trace = harness.trace_path(context)
        status, error = result.status.value, result.error
        trace_valid = verify_trace(trace).valid
        if status == "succeeded":
            claimed = predicted("team" if kind == "team" else "oneshot", result.final_output)
    except Exception as exc:
        # A bounded exception class only: provider bodies and credentials stay out.
        error = type(exc).__name__
    finally:
        await client.aclose()
    measured = [r for r in meter.responses if r["complete"]]
    statuses = [r["status"] for r in meter.responses]
    missing = meter.requests - len(meter.responses)
    calls = [r["seconds"] for r in meter.responses if r["seconds"] is not None]
    row = {
        "unit": unit.id,
        "kind": kind,
        "materials": unit.size,
        "status": status,
        "error": error,
        "claimed": claimed,
        "trace_valid": trace_valid,
        "seconds": time.monotonic() - started,
        "model_calls": meter.requests,
        "prompt_tokens": sum(r["prompt"] for r in measured),
        "cached_prompt_tokens": sum(r["cached"] for r in measured),
        "completion_tokens": sum(r["completion"] for r in measured),
        "max_prompt_tokens": max((r["prompt"] for r in measured), default=0),
        "fences_stripped": fences[0],
        "usage_known": args.mode == "live" and len(measured) == meter.requests > 0,
        "http_statuses": statuses,
        "missing_responses": missing,
        "call_seconds": calls,
        "infrastructure_error": args.mode == "live"
        and (missing > 0 or any(status >= 400 for status in statuses)),
        "infrastructure_retryable": args.mode == "live"
        and (missing > 0 or any(status == 429 or status >= 500 for status in statuses))
        and not any(400 <= status < 500 and status != 429 for status in statuses),
    }
    write(destination / "unit.json", row)
    return row


async def run_unit_measured(unit: Case, kind: str, args: Any, destination: Path) -> dict[str, Any]:
    """Rerun provider-side failures of one unit from fresh state; keep every attempt."""
    discarded: list[dict[str, Any]] = []
    for attempt in range(INFRA_ATTEMPTS):
        row = await run_unit(unit, kind, args, destination / f"attempt-{attempt}")
        if not row["infrastructure_retryable"] or attempt == INFRA_ATTEMPTS - 1:
            break
        discarded.append(
            {
                "attempt": attempt,
                "error": row["error"],
                "http_statuses": row["http_statuses"],
                "missing_responses": row["missing_responses"],
                "seconds": row["seconds"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
            }
        )
        await asyncio.sleep(INFRA_BACKOFF_SECONDS[min(attempt, len(INFRA_BACKOFF_SECONDS) - 1)])
    return {**row, "attempt": attempt, "discarded_attempts": discarded}


def cost(prompt: int, cached: int, completion: int) -> float:
    return (
        (prompt - cached) * PRICE_PER_MILLION["input_miss"]
        + cached * PRICE_PER_MILLION["input_hit"]
        + completion * PRICE_PER_MILLION["output"]
    ) / 1_000_000


async def run_arm(case: Case, arm: str, args: Any, destination: Path) -> dict[str, Any]:
    if arm == "oneshot":
        units, kind, in_flight = [case], "oneshot", 1
    elif arm == "split-32":
        units, kind, in_flight = groups(case, SPLIT_GROUP), "oneshot", SPLIT_IN_FLIGHT
    else:
        units, kind, in_flight = groups(case, TEAM_GROUP), "team", TEAM_PLANS_IN_FLIGHT
    gate = asyncio.Semaphore(in_flight)

    async def one(unit: Case) -> dict[str, Any]:
        async with gate:
            return await run_unit_measured(unit, kind, args, destination / "units" / unit.id)

    started = time.monotonic()
    unit_rows = await asyncio.gather(*(one(unit) for unit in units))
    seconds = time.monotonic() - started
    expected = set(case.violations)
    claimed = {key for row in unit_rows for key in row["claimed"] or []}
    failed = [row["unit"] for row in unit_rows if row["claimed"] is None]
    misses, alarms = sorted(expected - claimed), sorted(claimed - expected)
    prompt = sum(row["prompt_tokens"] for row in unit_rows)
    cached = sum(row["cached_prompt_tokens"] for row in unit_rows)
    completion = sum(row["completion_tokens"] for row in unit_rows)
    calls = [s for row in unit_rows for s in row["call_seconds"]]
    trace_valid = all(row["trace_valid"] for row in unit_rows)
    row = {
        "case": case.id,
        "size": case.size,
        "arm": arm,
        "units": len(units),
        "failed_units": failed,
        "expected_count": len(expected),
        "claimed_count": len(claimed),
        "misses": misses,
        "false_alarms": alarms,
        "errors": len(misses) + len(alarms),
        "judgements": case.size,
        "success": not misses and not alarms and not failed and trace_valid,
        "trace_valid": trace_valid,
        "seconds": seconds,
        "model_calls": sum(row["model_calls"] for row in unit_rows),
        "prompt_tokens": prompt,
        "cached_prompt_tokens": cached,
        "completion_tokens": completion,
        "max_prompt_tokens": max(row["max_prompt_tokens"] for row in unit_rows),
        "cost_usd_peak": cost(prompt, cached, completion),
        "fences_stripped": sum(row["fences_stripped"] for row in unit_rows),
        "usage_known": all(row["usage_known"] for row in unit_rows),
        "max_call_seconds": max(calls, default=None),
        "call_seconds_p50": percentile(calls, 0.5),
        "stalled_calls": sum(s > STALL_SECONDS for s in calls),
        "infrastructure_error": any(row["infrastructure_error"] for row in unit_rows),
        "infrastructure_retries": sum(len(row["discarded_attempts"]) for row in unit_rows),
        "unit_rows": [
            {k: v for k, v in unit_row.items() if k != "call_seconds"} for unit_row in unit_rows
        ],
    }
    write(destination / "result.json", row)
    return row


# ---------------------------------------------------------------------- summary


def judge(arms: dict[str, Any], size: int) -> dict[str, Any]:
    """Apply RULE at one N."""
    if not set(ARMS) <= set(arms):
        return {"incomplete": True}
    oneshot, split, team = arms["oneshot"], arms["split-32"], arms["team-16"]
    degrades = size > 32 and oneshot["errors"] >= 10 and oneshot["error_rate"] >= 0.005

    def fanout(arm: dict[str, Any]) -> str:
        if oneshot["errors"] < 10:
            return "no_quality_case"
        return "worth_it" if arm["errors"] <= oneshot["errors"] / 2 else "not_worth_it"

    if split["errors"] >= 10 and team["errors"] <= split["errors"] / 2:
        framework = "framework_better"
    elif team["errors"] >= 10 and split["errors"] <= team["errors"] / 2:
        framework = "program_better"
    else:
        framework = "no_clear_difference"
    return {
        "oneshot_degrades": degrades,
        "fanout": {"split-32": fanout(split), "team-16": fanout(team)},
        "framework_vs_program": framework,
    }


def summarize(rows: list[dict[str, Any]], expected_runs: int, mode: str) -> dict[str, Any]:
    by_size: dict[str, Any] = {}
    for size in SIZES:
        arms: dict[str, Any] = {}
        for arm in ARMS:
            selected = [r for r in rows if r["size"] == size and r["arm"] == arm]
            if not selected:
                continue
            judgements = sum(r["judgements"] for r in selected)
            errors = sum(r["errors"] for r in selected)
            arms[arm] = {
                "runs": len(selected),
                "successes": sum(r["success"] for r in selected),
                "judgements": judgements,
                "misses": sum(len(r["misses"]) for r in selected),
                "false_alarms": sum(len(r["false_alarms"]) for r in selected),
                "errors": errors,
                "error_rate": errors / judgements,
                "errors_by_case": {r["case"]: r["errors"] for r in selected},
                "failed_units": sum(len(r["failed_units"]) for r in selected),
                "latency_p50_seconds": percentile([r["seconds"] for r in selected], 0.5),
                "mean_model_calls": statistics.mean(r["model_calls"] for r in selected),
                "mean_max_prompt_tokens": statistics.mean(r["max_prompt_tokens"] for r in selected),
                "cost_usd_peak_per_run": statistics.mean(r["cost_usd_peak"] for r in selected),
                "stalled_calls": sum(r["stalled_calls"] for r in selected),
                "infrastructure_retries": sum(r["infrastructure_retries"] for r in selected),
                "fences_stripped": sum(r["fences_stripped"] for r in selected),
            }
        by_size[str(size)] = {"arms": arms, "judgement": judge(arms, size)}
    infra = sum(r["infrastructure_error"] for r in rows)
    complete = (
        len(rows) == expected_runs and len({(r["case"], r["arm"]) for r in rows}) == expected_runs
    )
    return {
        "schema_version": 1,
        "mode": mode,
        "expected_runs": expected_runs,
        "actual_runs": len(rows),
        "complete": complete,
        "infrastructure_failures": infra,
        "infrastructure_retries": sum(r["infrastructure_retries"] for r in rows),
        "failed_units": sum(len(r["failed_units"]) for r in rows),
        "measurement": mode == "live"
        and complete
        and infra == 0
        and all(r["trace_valid"] for r in rows)
        and all(r["usage_known"] for r in rows),
        "rule": RULE,
        "by_size": by_size,
        "degrades_at": [
            int(size)
            for size, entry in by_size.items()
            if entry["judgement"].get("oneshot_degrades")
        ],
        "price_basis": {"usd_per_million_tokens_peak": PRICE_PER_MILLION},
        "limits": [
            "synthetic author-generated audit tasks; independent materials; not business data",
            "one model at temperature 0; 4 tasks per N, one sample each",
            "ReAct and inputs beyond the provider context window are out of scope",
        ],
    }


# ---------------------------------------------------------------------- driver


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
    selected = long_cases(args.split)
    if args.sizes:
        selected = tuple(c for c in selected if c.size in args.sizes)
    expected_runs = len(selected) * len(ARMS)
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
        "protocol_sha256": digest(protocol) if protocol else None,
        "cases": [c.id for c in selected],
    }
    write(args.output / "manifest.json", manifest)
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(selected):
        # Rotate arm order so no arm always runs first against a warm cache.
        offset = index % len(ARMS)
        for arm in (*ARMS[offset:], *ARMS[:offset]):
            row = await run_arm(case, arm, args, args.output / case.id / arm)
            rows.append(row)
            print(
                f"{len(rows)}/{expected_runs} {case.id} {arm:<8} "
                f"errors={row['errors']}/{row['judgements']} "
                f"failed_units={len(row['failed_units'])} {row['seconds']:.1f}s "
                f"calls={row['model_calls']}",
                flush=True,
            )
    summary = summarize(rows, expected_runs, args.mode)
    summary["source_unchanged"] = snapshot(ROOT)["tree_digest"] == manifest["source_identity"]
    summary["measurement"] = summary["measurement"] and summary["source_unchanged"]
    summary["manifest_sha256"] = digest(manifest)
    write(args.output / "summary.json", summary)
    for size, entry in summary["by_size"].items():
        errors = {arm: f"{a['errors']}/{a['judgements']}" for arm, a in entry["arms"].items()}
        print(f"N={size}: errors {errors} -> {json.dumps(entry['judgement'])}", flush=True)
    return summary


def seal(args: Any) -> None:
    summary = json.loads((args.dev_report / "summary.json").read_text())
    manifest = json.loads((args.dev_report / "manifest.json").read_text())
    if not summary["complete"] or manifest["mode"] != "live" or manifest["split"] != "dev":
        raise ValueError("need a complete live dev run")
    if manifest["identity"] != identity():
        raise ValueError("the dev run used a different runner or corpus")
    # Pipeline health, not results: every unit must reach the provider cleanly and
    # answer parseably before the evaluation budget is spent.
    if not summary["measurement"]:
        raise ValueError("the dev run is not a clean measurement; inspect it before sealing")
    if summary["failed_units"]:
        raise ValueError("a dev unit produced no parseable answer; inspect it before sealing")
    if args.protocol is None or args.protocol.exists():
        raise ValueError("choose a new protocol path; protocols are immutable")
    write(
        args.protocol,
        {
            "schema_version": 1,
            "identity": identity(),
            "model": manifest["model"],
            "evaluation_cases": [c.id for c in long_cases("evaluation")],
            "planned_runs": len(long_cases("evaluation")) * len(ARMS),
            "dev_summary_sha256": digest(summary),
            "rule": RULE,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "evaluation"), default="dev")
    parser.add_argument("--mode", choices=("contract", "live"), default="contract")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--sizes", type=int, nargs="*", choices=SIZES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("--dev-report", type=Path)
    args = parser.parse_args()
    if args.seal:
        if args.dev_report is None:
            parser.error("--seal requires --dev-report")
        try:
            seal(args)
        except ValueError as exc:
            # A refusal stops a chained `dev && seal && evaluation` command line.
            parser.exit(2, f"seal refused: {exc}\n")
        print(f"sealed {args.protocol}", flush=True)
        return
    if args.output is None:
        parser.error("--output is required")
    try:
        asyncio.run(execute(args))
    except ValueError as exc:
        parser.exit(2, f"run refused: {exc}\n")


if __name__ == "__main__":
    main()

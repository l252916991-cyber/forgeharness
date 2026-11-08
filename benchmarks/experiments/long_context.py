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

import asyncio
import json
import statistics
from functools import partial
from pathlib import Path
from typing import Any

from benchmarks.experiments.common import (
    ANSWER_FORMAT,
    INFRASTRUCTURE_RULE,
    PRICE_PER_MILLION,
    STALL_SECONDS,
    AcceptAll,
    Materials,
    OneShot,
    Provider,
    ReadOnly,
    Study,
    cli,
    context_for,
    cost,
    file_sha256,
    finish,
    measured,
    oneshot_budget,
    percentile,
    predicted,
    run_team,
    start,
    team_plan,
    timed,
    write,
)
from benchmarks.experiments.common import (
    seal as seal_study,
)
from benchmarks.experiments.provider_guard import AccountRefused, ProviderGuard
from evals.audit_tasks import LONG_SIZES as SIZES
from evals.audit_tasks import RULES, Case, long_cases
from forgeharness.sdk import ApplicationSpec, Harness, verify_trace

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
    "infrastructure": INFRASTRUCTURE_RULE + " (per unit: the single call, a group or a plan)",
    "scope": "exploratory: one model, temperature 0, 4 tasks per N, one sample each",
}


def identity() -> dict[str, Any]:
    return {
        "files": file_sha256(
            "benchmarks/experiments/long_context.py",
            "benchmarks/experiments/common.py",
            "evals/audit_tasks.py",
        ),
        "output_tokens": OUTPUT_TOKENS,
        "groups": {"split": SPLIT_GROUP, "team": TEAM_GROUP},
        "in_flight": {"split": SPLIT_IN_FLIGHT, "team_plans": TEAM_PLANS_IN_FLIGHT},
    }


STUDY = Study(
    name="long-context",
    identity=identity,
    cases=long_cases,
    evaluation_split="evaluation",
    output_tokens=OUTPUT_TOKENS,
)


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


async def run_unit(
    unit: Case, kind: str, args: Any, destination: Path, *, guard: ProviderGuard
) -> dict[str, Any]:
    """One oneshot call or one collaboration plan over ``unit``, metered on its own."""
    provider = Provider(args, guard, OUTPUT_TOKENS, timeout=HTTP_TIMEOUT_SECONDS)
    run_id = f"{unit.id}-{kind}"
    elapsed = timed()
    status, error, claimed, trace_valid = "error", None, None, False
    try:
        if kind == "team":
            plan = team_plan(
                unit,
                f"{args.model}:t0:max{OUTPUT_TOKENS}",
                specialists=TEAM_SPECIALISTS,
                output_tokens=OUTPUT_TOKENS,
                version_prefix="long",
            )
            status, error, final, trace = await run_team(
                plan, lambda: provider.model(unit), destination, run_id, OUTPUT_TOKENS
            )
        else:
            state = destination / "state"
            state.mkdir(parents=True, mode=0o700)
            tool = Materials(unit)
            spec = ApplicationSpec(
                id="scaling-study",
                version="1",
                policy_version="1",
                model_factory=lambda: provider.model(unit),
                tools=(tool,),
                policy=ReadOnly(),
                verifier=AcceptAll(),
                budget=oneshot_budget(
                    unit.size, OUTPUT_TOKENS, max_context_tokens=900_000, wall_seconds=1_800
                ),
                instructions=(RULES + "\n" + ANSWER_FORMAT,),
            )
            harness = Harness(spec, state_dir=state, orchestrator=OneShot(list(tool.values)))
            context = context_for(spec.id, "1", "1", run_id)
            task = "Service IDs: " + ", ".join(tool.values)
            result = await harness.run(
                context=context, task=task + "\nRead each service with read_material."
            )
            status, error, final = result.status.value, result.error, result.final_output
            trace = harness.trace_path(context)
        trace_valid = verify_trace(trace).valid
        if status == "succeeded":
            claimed = predicted("team" if kind == "team" else "oneshot", final)
    except Exception as exc:
        # A bounded exception class only: provider bodies and credentials stay out.
        error = type(exc).__name__
    finally:
        await provider.close()
    return {
        "unit": unit.id,
        "kind": kind,
        "materials": unit.size,
        "status": status,
        "error": error,
        "claimed": claimed,
        "trace_valid": trace_valid,
        "seconds": elapsed(),
        **provider.metrics(),
    }


async def run_arm(
    case: Case, arm: str, args: Any, destination: Path, *, guard: ProviderGuard
) -> dict[str, Any]:
    if arm == "oneshot":
        units, kind, in_flight = [case], "oneshot", 1
    elif arm == "split-32":
        units, kind, in_flight = groups(case, SPLIT_GROUP), "oneshot", SPLIT_IN_FLIGHT
    else:
        units, kind, in_flight = groups(case, TEAM_GROUP), "team", TEAM_PLANS_IN_FLIGHT
    gate = asyncio.Semaphore(in_flight)

    async def one(unit: Case) -> dict[str, Any]:
        async with gate:
            guard.check()
            return await measured(
                partial(run_unit, unit, kind, args, guard=guard), destination / "units" / unit.id
            )

    elapsed = timed()
    unit_rows = await asyncio.gather(*(one(unit) for unit in units))
    seconds = elapsed()
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


async def execute(args: Any) -> dict[str, Any]:
    manifest, _ = start(STUDY, args)
    selected = long_cases(args.split)
    if args.sizes:
        selected = tuple(c for c in selected if c.size in args.sizes)
    expected_runs = len(selected) * len(ARMS)
    manifest |= {"cases": [c.id for c in selected]}
    write(args.output / "manifest.json", manifest)
    guard = ProviderGuard()
    rows: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(selected):
            # Rotate arm order so no arm always runs first against a warm cache.
            offset = index % len(ARMS)
            for arm in (*ARMS[offset:], *ARMS[:offset]):
                guard.check()
                row = await run_arm(case, arm, args, args.output / case.id / arm, guard=guard)
                rows.append(row)
                print(
                    f"{len(rows)}/{expected_runs} {case.id} {arm:<8} "
                    f"errors={row['errors']}/{row['judgements']} "
                    f"failed_units={len(row['failed_units'])} {row['seconds']:.1f}s "
                    f"calls={row['model_calls']}",
                    flush=True,
                )
    except AccountRefused as exc:
        print(f"stopped: {exc}", flush=True)
    summary: dict[str, Any] = finish(
        args, manifest, summarize(rows, expected_runs, args.mode), guard
    )
    for size, entry in summary["by_size"].items():
        errors = {arm: f"{a['errors']}/{a['judgements']}" for arm, a in entry["arms"].items()}
        print(f"N={size}: errors {errors} -> {json.dumps(entry['judgement'])}", flush=True)
    return summary


def seal(args: Any) -> None:
    def checks(summary: dict[str, Any]) -> None:
        if summary["failed_units"]:
            raise ValueError("a dev unit produced no parseable answer; inspect it before sealing")

    seal_study(
        STUDY,
        args,
        {
            "evaluation_cases": [c.id for c in long_cases("evaluation")],
            "planned_runs": len(long_cases("evaluation")) * len(ARMS),
            "rule": RULE,
        },
        checks,
    )


def main() -> None:
    def configure(parser: Any) -> None:
        parser.add_argument("--sizes", type=int, nargs="*", choices=SIZES)

    cli(STUDY, __doc__ or "", execute, seal, configure)


if __name__ == "__main__":
    main()

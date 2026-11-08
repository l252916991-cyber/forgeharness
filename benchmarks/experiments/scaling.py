"""When do fixed specialists beat one agent? A scaling study over material count.

Three arms audit the same N independent service configurations (N = 4, 8, 16, 32)
with the same model, rules and answer format:

- ``react``: one ReAct agent that reads each service with ``read_material``;
- ``oneshot``: every service read deterministically, then one model call;
- ``team``: the fixed-specialist ``Collaboration``: up to eight specialists, N/8
  services each, at most two concurrent model calls, then a coordinator.

Run ``--split dev`` first, ``--seal`` a protocol from it, then ``--split evaluation``
once with that protocol. ``--mode contract`` swaps in an oracle model for wiring tests
and never counts as a measurement. Report: ``reports/collaboration-scaling``.
"""

from __future__ import annotations

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
    file_sha256,
    finish,
    grade,
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
from evals.audit_tasks import RULES, SIZES, Case, cases
from forgeharness.sdk import ApplicationSpec, Harness, RunBudget, verify_trace

ARMS = ("react", "oneshot", "team")
MAX_SPECIALISTS = 8
OUTPUT_TOKENS = 1024
SAMPLES = 2
DECISION_RULE = {
    "metric": "exact-set success rate per material count N",
    "best_single": "the higher of react and oneshot success rates; ties go to lower cost",
    "team_worth_it_if": [
        "team success >= best single AND (team p50 latency <= 0.8x OR cost per run <= 0.8x)",
        "team success - best single success >= 0.25",
    ],
    "scope": "exploratory: 4 tasks x 2 samples per arm and N; no multiplicity correction",
    "infrastructure": INFRASTRUCTURE_RULE,
    "secondary": (
        "per-call provider latency, and calls slower than 30 s, are reported descriptively; "
        "they are not part of the decision"
    ),
}


def identity() -> dict[str, Any]:
    return {
        "files": file_sha256(
            "benchmarks/experiments/scaling.py",
            "benchmarks/experiments/common.py",
            "evals/audit_tasks.py",
        ),
        "output_tokens": OUTPUT_TOKENS,
        "max_specialists": MAX_SPECIALISTS,
    }


STUDY = Study(
    name="scaling",
    identity=identity,
    cases=cases,
    evaluation_split="evaluation",
    output_tokens=OUTPUT_TOKENS,
)


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


def plan_for(case: Case, model: str) -> Any:
    return team_plan(
        case, f"{model}:t0:max{OUTPUT_TOKENS}", specialists=MAX_SPECIALISTS,
        output_tokens=OUTPUT_TOKENS,
    )  # fmt: skip


async def run_one(
    case: Case, arm: str, sample: int, args: Any, destination: Path, *, guard: ProviderGuard
) -> dict[str, Any]:
    provider = Provider(args, guard, OUTPUT_TOKENS, timeout=120)
    tool = Materials(case)
    run_id = f"{case.id}-{arm}-{sample}"
    elapsed = timed()
    final: str | None = None
    status, error, trace_valid = "error", None, False
    try:
        if arm == "team":
            status, error, final, trace = await run_team(
                plan_for(case, args.model),
                lambda: provider.model(case),
                destination,
                run_id,
                OUTPUT_TOKENS,
            )
        else:
            state = destination / "state"
            state.mkdir(parents=True, mode=0o700)
            spec = ApplicationSpec(
                id="scaling-study",
                version="1",
                policy_version="1",
                model_factory=lambda: provider.model(case),
                tools=(tool,),
                policy=ReadOnly(),
                verifier=AcceptAll(),
                budget=(
                    react_budget(case.size)
                    if arm == "react"
                    else oneshot_budget(case.size, OUTPUT_TOKENS)
                ),
                instructions=(RULES + "\n" + ANSWER_FORMAT,),
            )
            harness = Harness(
                spec,
                state_dir=state,
                orchestrator=OneShot(list(tool.values)) if arm == "oneshot" else None,
            )
            context = context_for(spec.id, "1", "1", run_id)
            task = "Service IDs: " + ", ".join(tool.values)
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
        await provider.close()
    claimed = predicted(arm, final) if status == "succeeded" else None
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
        "seconds": elapsed(),
        "material_reads": len(tool.calls),
        **provider.metrics(),
    }
    row["success"] = bool(row["success"] and trace_valid)
    write(destination / "result.json", row)
    return row


def summarize(rows: list[dict[str, Any]], expected_runs: int, mode: str) -> dict[str, Any]:
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
                # Descriptive only (DECISION_RULE "secondary").
                "max_call_seconds": max(
                    (r["max_call_seconds"] or 0.0 for r in selected), default=0.0
                ),
                "stalled_calls": sum(r["stalled_calls"] for r in selected),
                "infrastructure_retries": sum(len(r["discarded_attempts"]) for r in selected),
            }
        by_size[str(size)] = {"arms": arms, "decision": decide(arms)}
    infra = sum(r["infrastructure_error"] for r in rows)
    calls = sorted(seconds for r in rows for seconds in r["call_seconds"])
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
        "infrastructure_retries": sum(len(r["discarded_attempts"]) for r in rows),
        "provider_calls": {
            "count": len(calls),
            "p50_seconds": percentile(calls, 0.5),
            "p95_seconds": percentile(calls, 0.95),
            "max_seconds": calls[-1] if calls else None,
            "stalled": sum(seconds > STALL_SECONDS for seconds in calls),
        },
        "malformed_or_failed_runs": sum(r["claimed"] is None for r in rows),
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


async def execute(args: Any) -> dict[str, Any]:
    manifest, protocol = start(STUDY, args)
    if protocol is not None and protocol["samples"] != args.samples:
        raise ValueError("sample count differs from the sealed protocol")
    selected = cases(args.split)
    if args.sizes:
        selected = tuple(c for c in selected if c.size in args.sizes)
    expected_runs = len(selected) * len(ARMS) * args.samples
    manifest |= {"samples": args.samples, "cases": [c.id for c in selected]}
    write(args.output / "manifest.json", manifest)
    guard = ProviderGuard()
    rows: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(selected):
            for sample in range(args.samples):
                # Rotate arm order so no arm always runs first against a warm cache.
                offset = (index + sample) % len(ARMS)
                for arm in (*ARMS[offset:], *ARMS[:offset]):
                    guard.check()
                    destination = args.output / case.id / f"{arm}-{sample}"
                    row = await measured(
                        partial(run_one, case, arm, sample, args, guard=guard), destination
                    )
                    rows.append(row)
                    print(
                        f"{len(rows)}/{expected_runs} {case.id} {arm:<7} "
                        f"{'pass' if row['success'] else 'fail'} f1={row['f1']:.2f} "
                        f"{row['seconds']:.1f}s calls={row['model_calls']}",
                        flush=True,
                    )
    except AccountRefused as exc:
        print(f"stopped: {exc}", flush=True)
    summary: dict[str, Any] = finish(
        args, manifest, summarize(rows, expected_runs, args.mode), guard
    )
    for size, entry in summary["by_size"].items():
        rates = {arm: f"{a['successes']}/{a['runs']}" for arm, a in entry["arms"].items()}
        print(f"N={size}: {rates} -> {entry['decision']}", flush=True)
    return summary


def seal(args: Any) -> None:
    def checks(summary: dict[str, Any]) -> None:
        if summary["malformed_or_failed_runs"]:
            raise ValueError("a dev run produced no parseable answer; inspect it before sealing")

    seal_study(
        STUDY,
        args,
        {
            "samples": SAMPLES,
            "evaluation_cases": [c.id for c in cases("evaluation")],
            "planned_runs": len(cases("evaluation")) * len(ARMS) * SAMPLES,
            "decision_rule": DECISION_RULE,
        },
        checks,
    )


def main() -> None:
    def configure(parser: Any) -> None:
        parser.add_argument("--samples", type=int, choices=(1, 2), default=1)
        parser.add_argument("--sizes", type=int, nargs="*", choices=SIZES)

    cli(STUDY, __doc__ or "", execute, seal, configure)


if __name__ == "__main__":
    main()

"""Why did fixed specialists miss violations at N = 32? An ablation on fresh tasks.

In the scaling study (``benchmarks.experiments.scaling``) the team arm fell to
1/8 at N = 32 while one plain call over all 32 materials was 8/8. Every miss was a
specialist that had already declared the service compliant in its summary; the
coordinator merged faithfully. This study tests the candidate explanations on
twelve fresh N = 32 tasks (``evals.audit_tasks.ablation_cases``), one sample
each, with the same model, rules and grader:

- ``oneshot``: control, unchanged from the scaling study;
- ``team``: the scaling study's team arm, unchanged (8 specialists x 4 services);
- ``team-system-first``: the same team, but the provider receives every request with
  its system messages first (H2: a collaboration node places the run goal, a user
  message, ahead of the expert's system message). The framework rejects a prepared
  payload that differs from the recorded request, so this probe reorders inside the
  benchmark's model wrapper: the trace records the runtime order, the provider gets
  ``system_first`` of it, and the wrapper counts every request it changed;
- ``team-findings-first``: the same team; the specialist instruction adds one
  sentence asking for findings before the summary (H1: early commitment);
- ``team-load2``: two 8-specialist runs over the halves, 2 services per specialist,
  claims united by the program (H3: per-specialist load; a plan holds at most eight
  tasks, so 16 specialists cannot share one coordinator);
- ``split-oneshot``: the team's 8 groups of 4, each audited by one plain call in the
  oneshot format, answers united by the program (splitting without the
  collaboration framing).

Run ``--split dev`` (the scaling study's dev N = 32 task), ``--seal`` a protocol from
it, then ``--split ablation`` once with that protocol. ``--mode contract`` swaps in
the scaling study's oracle model for wiring tests and never counts as a measurement.
Report: ``reports/collaboration-ablation``.
"""

from __future__ import annotations

import json
import statistics
from functools import partial
from pathlib import Path
from typing import Any

from benchmarks.experiments.common import (
    ANSWER_FORMAT,
    INFRASTRUCTURE_RULE,
    PRICE_PER_MILLION,
    SPECIALIST,
    AcceptAll,
    Materials,
    OneShot,
    Provider,
    ReadOnly,
    Study,
    Unfenced,
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
    unfence,
    write,
)
from benchmarks.experiments.common import (
    seal as seal_study,
)
from benchmarks.experiments.provider_guard import AccountRefused, ProviderGuard
from evals.audit_tasks import RULES, Case, ablation_cases, cases
from forgeharness.sdk import (
    ApplicationSpec,
    FinalAction,
    Harness,
    Message,
    MessageRole,
    ModelRequest,
    ModelResult,
    RunBudget,
    RunKernel,
    RunStatus,
    ToolCall,
    verify_trace,
)

OUTPUT_TOKENS = 1024
ARMS = (
    "oneshot",
    "team",
    "team-system-first",
    "team-findings-first",
    "team-load2",
    "split-oneshot",
)
VARIANTS = ("team-system-first", "team-findings-first", "team-load2", "split-oneshot")
SPECIALIST_FINDINGS_FIRST = (
    SPECIALIST + " Write the findings field before the summary field; write the summary last."
)
ABLATION_RULE = {
    "tasks": "12 fresh N = 32 tasks (seed 3303), one sample each, every arm on every task",
    "primary": "exact-set success per arm; service-level errors (misses + false alarms) second",
    "replication_gate": (
        "attribution is attempted only if oneshot succeeds on >= 11/12 and team fails on "
        ">= 4/12; otherwise the scaling study's failure did not replicate"
    ),
    "manipulation_checks": (
        "team-system-first: the wrapper reorders every request it sees; "
        "team-findings-first: >= 90% of specialist reports put findings before summary; "
        "an arm whose check fails is not interpreted"
    ),
    "effect": (
        "a variant removes the failure if its successes exceed team's by >= 4 and its "
        "service-level errors are at most half of team's; it has no effect if its "
        "successes are within 2 of team's; anything else is inconclusive"
    ),
    "infrastructure": INFRASTRUCTURE_RULE,
    "scope": "exploratory: one model, temperature 0, 12 tasks; no multiplicity correction",
}
SAMPLES = 1


def identity() -> dict[str, Any]:
    return {
        "files": file_sha256(
            "benchmarks/experiments/ablation.py",
            "benchmarks/experiments/common.py",
            "evals/audit_tasks.py",
        ),
        "output_tokens": OUTPUT_TOKENS,
    }


def selected_cases(split: str) -> tuple[Case, ...]:
    """Dev is the scaling study's dev N = 32 task; ``ablation`` the twelve fresh tasks."""
    if split == "dev":
        return tuple(c for c in cases("dev") if c.size == 32)
    if split == "ablation":
        return ablation_cases()
    raise ValueError("split must be 'dev' or 'ablation'")


STUDY = Study(
    name="ablation",
    identity=identity,
    cases=selected_cases,
    evaluation_split="ablation",
    output_tokens=OUTPUT_TOKENS,
)


# ---------------------------------------------------------------------- arms


def halves(case: Case) -> tuple[Case, Case]:
    """Two sub-tasks of the first and second half; service IDs are unchanged."""
    middle = case.size // 2
    return (
        Case(id=f"{case.id}-a", split=case.split, size=middle, services=case.services[:middle]),
        Case(
            id=f"{case.id}-b",
            split=case.split,
            size=case.size - middle,
            services=case.services[middle:],
        ),
    )


def arm_plan(case: Case, arm: str, model_identity: str) -> Any:
    """The scaling study's team plan, with the variant's specialist text."""
    return team_plan(
        case,
        model_identity,
        specialists=8,
        output_tokens=OUTPUT_TOKENS,
        instructions=SPECIALIST_FINDINGS_FIRST if arm == "team-findings-first" else SPECIALIST,
    )


def system_first(request: ModelRequest) -> ModelRequest:
    """Every system message ahead of the others, each group in its original order."""
    system = [m for m in request.messages if m.role is MessageRole.SYSTEM]
    rest = [m for m in request.messages if m.role is not MessageRole.SYSTEM]
    return request.model_copy(update={"messages": (*system, *rest)})


class SystemFirst(Unfenced):
    """Send ``system_first`` of each request and count what changed.

    ``counts`` is ``[requests seen, requests reordered]``. The recorded request is the
    runtime order; the provider input is this deterministic function of it.
    """

    def __init__(self, inner: Any, counter: list[int], counts: list[int]) -> None:
        super().__init__(inner, counter)
        self._counts = counts

    async def decide(self, request: ModelRequest) -> ModelResult:
        sent = system_first(request)
        self._counts[0] += 1
        self._counts[1] += sent.messages != request.messages
        return self._clean(await self._inner.decide(sent))


class SplitOneShot:
    """The team's groups, each audited by one plain oneshot-format call; the program
    unites the answers and adds nothing else."""

    def __init__(self, keys: list[str], groups: list[tuple[str, ...]]) -> None:
        self.keys, self.groups = keys, groups

    async def run(self, *, kernel: RunKernel, task: str) -> Any:
        del task
        services: dict[str, str] = {}
        for index, key in enumerate(self.keys):
            output = await kernel.call_tool(
                ToolCall(id=f"read-{index}", name="read_material", arguments={"key": key})
            )
            if not output.ok:
                return await kernel.finish(status=RunStatus.FAILED, error="material read failed")
            services[key] = output.content
        claimed: set[str] = set()
        for number, group in enumerate(self.groups, start=1):
            group_task = (
                "Service IDs: " + ", ".join(group) + "\nRead each service with read_material."
            )
            result = await kernel.call_model(
                messages=(
                    Message(role=MessageRole.SYSTEM, content=RULES + "\n" + ANSWER_FORMAT),
                    Message(
                        role=MessageRole.USER,
                        content=json.dumps(
                            {"task": group_task, "services": {k: services[k] for k in group}},
                            ensure_ascii=False,
                        ),
                    ),
                ),
                phase=f"split-{number}",
                include_tools=False,
            )
            if not isinstance(result.action, FinalAction):
                return await kernel.finish(status=RunStatus.FAILED, error="tool action returned")
            ids = predicted("oneshot", result.action.content)
            if ids is None:
                return await kernel.finish(status=RunStatus.FAILED, error="malformed group answer")
            claimed.update(ids)
        answer = json.dumps({"violations": sorted(claimed)})
        return await kernel.finish(final=FinalAction(content=answer))


def split_budget(size: int, groups: int) -> RunBudget:
    return RunBudget(
        max_steps=groups + 1,
        max_tool_calls=size,
        max_input_tokens=200_000,
        max_output_tokens=OUTPUT_TOKENS * groups,
        max_context_tokens=128_000,
        max_wall_seconds=900,
        max_attempts_per_call=1,
    )


# ------------------------------------------------------------------ trace reading


def findings_first(content: str) -> bool:
    """Whether a raw specialist report writes its findings key before its summary key."""
    text, _ = unfence(content)
    findings, summary = text.find('"findings"'), text.find('"summary"')
    return findings != -1 and (summary == -1 or findings < summary)


def team_breakdown(trace: Path) -> dict[str, Any]:
    """Specialist versus coordinator claims, and both manipulation checks, from a trace."""
    specialist: set[str] = set()
    coordinator: set[str] | None = None
    order: list[bool] = []
    requests: list[bool] = []
    for line in trace.read_text().splitlines():
        event = json.loads(line)["event"]
        payload = event["payload"]
        node = str(payload.get("phase", "")).removeprefix("r2:")
        if event["type"] == "model.request" and node.startswith("specialist."):
            # The H2 premise, in the runtime order: a system message after a user one.
            roles = [m["role"] for m in payload["messages"]]
            first_other = next((i for i, r in enumerate(roles) if r != "system"), len(roles))
            requests.append("system" in roles[first_other:])
        if event["type"] != "model.action" or payload["action"].get("kind") != "final":
            continue
        content = payload["action"]["content"]
        claimed = predicted("team", unfence(content)[0]) or []
        if payload["node_id"] == "coordinator":
            coordinator = set(claimed)
        else:
            specialist.update(claimed)
            order.append(findings_first(content))
    return {
        "specialist_claimed": sorted(specialist),
        "coordinator_dropped": sorted(specialist - (coordinator or set())),
        "coordinator_added": sorted((coordinator or set()) - specialist),
        "specialist_reports": len(order),
        "findings_first_reports": sum(order),
        "specialist_requests": len(requests),
        "system_after_user_requests": sum(requests),
    }


def merge_breakdowns(parts: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            merged[key] = (
                sorted(set(merged.get(key, [])) | set(value))
                if isinstance(value, list)
                else merged.get(key, 0) + value
            )
    return merged


# ---------------------------------------------------------------------- one run


async def run_one(
    case: Case, arm: str, args: Any, destination: Path, *, guard: ProviderGuard
) -> dict[str, Any]:
    provider = Provider(args, guard, OUTPUT_TOKENS, timeout=120)
    reorder = [0, 0]

    def model_for(task_case: Case) -> Any:
        if arm == "team-system-first":
            return SystemFirst(provider.inner(task_case), provider.fences, reorder)
        return Unfenced(provider.inner(task_case), provider.fences)

    tool = Materials(case)
    run_id = f"{case.id}-{arm}-0"
    model_identity = f"{args.model}:t0:max{OUTPUT_TOKENS}"
    elapsed = timed()
    status, error, trace_valid = "error", None, False
    breakdown: dict[str, Any] | None = None
    claimed: list[str] | None = None
    try:
        if arm.startswith("team"):
            # team-load2 runs two halves; every other team arm runs the whole case once.
            parts = (
                [(half, destination / half.id.rsplit("-", 1)[1]) for half in halves(case)]
                if arm == "team-load2"
                else [(case, destination)]
            )
            outcomes = [
                await run_team(
                    arm_plan(part, arm, model_identity),
                    partial(model_for, part),
                    folder,
                    f"{run_id}-{part.id}",
                    OUTPUT_TOKENS,
                )
                for part, folder in parts
            ]
            statuses = {outcome[0] for outcome in outcomes}
            status = (
                "succeeded" if statuses == {"succeeded"} else sorted(statuses - {"succeeded"})[0]
            )
            error = next((outcome[1] for outcome in outcomes if outcome[1]), None)
            answers = [predicted("team", outcome[2]) for outcome in outcomes]
            if status == "succeeded" and all(answer is not None for answer in answers):
                claimed = sorted({key for answer in answers for key in answer or []})
            trace_valid = all(verify_trace(outcome[3]).valid for outcome in outcomes)
            breakdown = merge_breakdowns([team_breakdown(outcome[3]) for outcome in outcomes])
        else:
            state = destination / "state"
            state.mkdir(parents=True, mode=0o700)
            groups = [t.evidence_ids for t in arm_plan(case, "team", model_identity).tasks]
            spec = ApplicationSpec(
                id="scaling-study",
                version="1",
                policy_version="1",
                model_factory=partial(model_for, case),
                tools=(tool,),
                policy=ReadOnly(),
                verifier=AcceptAll(),
                budget=(
                    oneshot_budget(case.size, OUTPUT_TOKENS)
                    if arm == "oneshot"
                    else split_budget(case.size, len(groups))
                ),
                instructions=(RULES + "\n" + ANSWER_FORMAT,),
            )
            orchestrator = (
                OneShot(list(tool.values))
                if arm == "oneshot"
                else SplitOneShot(list(tool.values), groups)
            )
            harness = Harness(spec, state_dir=state, orchestrator=orchestrator)
            context = context_for(spec.id, "1", "1", run_id)
            task = "Service IDs: " + ", ".join(tool.values)
            result = await harness.run(
                context=context, task=task + "\nRead each service with read_material."
            )
            status, error = result.status.value, result.error
            trace_valid = verify_trace(harness.trace_path(context)).valid
            claimed = predicted("oneshot", result.final_output) if status == "succeeded" else None
    except Exception as exc:
        # A bounded exception class only: provider bodies and credentials stay out.
        error, claimed = type(exc).__name__, None
    finally:
        await provider.close()
    expected = set(case.violations)
    row = {
        "case": case.id,
        "size": case.size,
        "arm": arm,
        "sample": 0,
        "status": status,
        "error": error,
        "expected": list(case.violations),
        "claimed": claimed,
        **grade(case.violations, claimed),
        "misses": sorted(expected - set(claimed or [])) if claimed is not None else None,
        "false_alarms": sorted(set(claimed or []) - expected) if claimed is not None else None,
        "team_breakdown": breakdown,
        "reordered_requests": reorder[1] if arm == "team-system-first" else None,
        "requests_seen_by_reorder": reorder[0] if arm == "team-system-first" else None,
        "trace_valid": trace_valid,
        "seconds": elapsed(),
        "material_reads": len(tool.calls),
        **provider.metrics(),
    }
    row["success"] = bool(row["success"] and trace_valid)
    write(destination / "result.json", row)
    return row


# ---------------------------------------------------------------------- summary


def manipulation_ok(arm: str, data: dict[str, Any]) -> bool | None:
    if arm == "team-system-first":
        seen, reordered = int(data["requests_seen_by_reorder"]), int(data["reordered_requests"])
        return seen > 0 and reordered == seen
    if arm == "team-findings-first":
        reports, first = int(data["specialist_reports"]), int(data["findings_first_reports"])
        return reports > 0 and first >= 0.9 * reports
    return None


def attribute(arms: dict[str, Any]) -> dict[str, Any]:
    """Apply ABLATION_RULE to per-arm summaries."""
    if not set(ARMS) <= set(arms):
        return {"replicated": None, "variants": {}}
    oneshot, team = arms["oneshot"], arms["team"]
    replicated = oneshot["successes"] >= oneshot["runs"] - 1 and (
        team["runs"] - team["successes"] >= 4
    )
    variants: dict[str, str] = {}
    for arm in VARIANTS:
        data = arms[arm]
        if not replicated:
            variants[arm] = "not_attributed:no_replication"
        elif data.get("manipulation_ok") is False:
            variants[arm] = "not_interpreted:manipulation_failed"
        elif data["successes"] - team["successes"] >= 4 and (
            data["service_errors"] <= team["service_errors"] / 2
        ):
            variants[arm] = "removes_failure"
        elif abs(data["successes"] - team["successes"]) <= 2:
            variants[arm] = "no_effect"
        else:
            variants[arm] = "inconclusive"
    return {"replicated": replicated, "variants": variants}


def summarize(rows: list[dict[str, Any]], expected_runs: int, mode: str) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    for arm in ARMS:
        selected = [r for r in rows if r["arm"] == arm]
        if not selected:
            continue
        successes = sum(r["success"] for r in selected)
        parsed = [r for r in selected if r["claimed"] is not None]
        misses = sum(len(r["misses"]) for r in parsed)
        alarms = sum(len(r["false_alarms"]) for r in parsed)
        data: dict[str, Any] = {
            "runs": len(selected),
            "successes": successes,
            "success_rate": successes / len(selected),
            "misses": misses,
            "false_alarms": alarms,
            "service_errors": misses + alarms,
            "judgements": sum(r["size"] for r in parsed),
            "mean_f1": statistics.mean(r["f1"] for r in selected),
            "latency_p50_seconds": percentile([r["seconds"] for r in selected], 0.5),
            "mean_model_calls": statistics.mean(r["model_calls"] for r in selected),
            "mean_max_prompt_tokens": statistics.mean(r["max_prompt_tokens"] for r in selected),
            "cost_usd_peak_per_run": sum(r["cost_usd_peak"] for r in selected) / len(selected),
            "malformed_or_failed_runs": len(selected) - len(parsed),
            "fences_stripped": sum(r["fences_stripped"] for r in selected),
            "stalled_calls": sum(r["stalled_calls"] for r in selected),
            "infrastructure_retries": sum(len(r["discarded_attempts"]) for r in selected),
        }
        breakdowns = [r["team_breakdown"] for r in selected if r["team_breakdown"]]
        if arm.startswith("team"):
            for key in (
                "specialist_reports",
                "findings_first_reports",
                "specialist_requests",
                "system_after_user_requests",
            ):
                data[key] = sum(b[key] for b in breakdowns)
            data["specialist_errors"] = sum(
                len(set(r["team_breakdown"]["specialist_claimed"]) ^ set(r["expected"]))
                for r in selected
                if r["team_breakdown"]
            )
            data["coordinator_dropped"] = sum(len(b["coordinator_dropped"]) for b in breakdowns)
            data["coordinator_added"] = sum(len(b["coordinator_added"]) for b in breakdowns)
            for key in ("requests_seen_by_reorder", "reordered_requests"):
                data[key] = sum(r[key] or 0 for r in selected)
            data["manipulation_ok"] = manipulation_ok(arm, data)
        arms[arm] = data
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
        "infrastructure_retries": sum(len(r["discarded_attempts"]) for r in rows),
        "malformed_or_failed_runs": sum(r["claimed"] is None for r in rows),
        "measurement": mode == "live"
        and complete
        and infra == 0
        and all(r["trace_valid"] for r in rows)
        and all(r["usage_known"] for r in rows),
        "rule": ABLATION_RULE,
        "arms": arms,
        "attribution": attribute(arms),
        "price_basis": {"usd_per_million_tokens_peak": PRICE_PER_MILLION},
        "limits": [
            "synthetic author-generated audit tasks; not business data",
            "one model at temperature 0; one sample per task",
            "12 tasks; exploratory",
        ],
    }


async def execute(args: Any) -> dict[str, Any]:
    manifest, _ = start(STUDY, args)
    selected = selected_cases(args.split)
    expected_runs = len(selected) * len(ARMS) * SAMPLES
    manifest |= {"samples": SAMPLES, "cases": [c.id for c in selected]}
    write(args.output / "manifest.json", manifest)
    guard = ProviderGuard()
    rows: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(selected):
            # Rotate arm order so no arm always runs first against a warm cache.
            offset = index % len(ARMS)
            for arm in (*ARMS[offset:], *ARMS[:offset]):
                guard.check()
                row = await measured(
                    partial(run_one, case, arm, args, guard=guard),
                    args.output / case.id / f"{arm}-0",
                )
                rows.append(row)
                print(
                    f"{len(rows)}/{expected_runs} {case.id} {arm:<19} "
                    f"{'pass' if row['success'] else 'fail'} f1={row['f1']:.2f} "
                    f"{row['seconds']:.1f}s calls={row['model_calls']}",
                    flush=True,
                )
    except AccountRefused as exc:
        print(f"stopped: {exc}", flush=True)
    summary: dict[str, Any] = finish(
        args, manifest, summarize(rows, expected_runs, args.mode), guard
    )
    for arm, data in summary["arms"].items():
        print(
            f"{arm:<19} {data['successes']}/{data['runs']} service_errors={data['service_errors']}",
            flush=True,
        )
    print(f"attribution: {json.dumps(summary['attribution'])}", flush=True)
    return summary


def seal(args: Any) -> None:
    def checks(summary: dict[str, Any]) -> None:
        if summary["malformed_or_failed_runs"]:
            raise ValueError("a dev run produced no parseable answer; inspect it before sealing")
        failed = [arm for arm, d in summary["arms"].items() if d.get("manipulation_ok") is False]
        if failed:
            raise ValueError(f"manipulation check failed in dev for {failed}; inspect it")

    seal_study(
        STUDY,
        args,
        {
            "samples": SAMPLES,
            "ablation_cases": [c.id for c in ablation_cases()],
            "planned_runs": len(ablation_cases()) * len(ARMS) * SAMPLES,
            "rule": ABLATION_RULE,
        },
        checks,
    )


def main() -> None:
    cli(STUDY, __doc__ or "", execute, seal)


if __name__ == "__main__":
    main()

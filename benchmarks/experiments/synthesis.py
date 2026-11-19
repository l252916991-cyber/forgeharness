"""Does collaboration earn its place when the answer needs more than one material?

Three earlier studies (scaling, ablation, long context) used independent materials:
the answer was a union of per-material judgements, a single call was perfect up to
about 50k tokens, and past that a plain program split was as accurate as the
collaboration framework at a third of the cost. All three reports name the case they
did not cover: a task whose answer combines materials. This study is that case. The
plan, including the rule below, is ``reports/cross-material-synthesis/PLAN.md``.

Task (``evals.synthesis_tasks``): the audit rules R1-R3 plus R4, a service that
breaks none of them is *at risk* when a service it currently calls breaks one. Every
service is labelled violation, at-risk or compliant.

Arms, all with the same model, rules and output limit, at N = 16 and 32:

- ``oneshot``: every material read deterministically, then one model call;
- ``split-naive``: groups of 8, each exactly the oneshot arm over its group, labels
  united. It cannot see calls that cross groups: the manipulation check;
- ``split-extract``: groups of 8, each asked for its R1-R3 violators and each
  service's ``calls_upstream``; the program applies R4 over the union. The strongest
  program baseline: the model extracts, code combines;
- ``team-s1``: ``Collaboration`` with 8 materials per specialist and a coordinator,
  report schema 1; specialists are asked for one finding per service;
- ``team-s2``: the same team with schema 2 (ADR 051): one enforced assessment per
  service before any conclusion.

Run ``--split dev``, ``--seal`` a protocol from it, then ``--split evaluation`` once.
``--mode contract`` swaps in an oracle that answers what each unit can see; it checks
the wiring (``split-naive`` must lose exactly the hidden at-risk services) and never
counts as a measurement.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from benchmarks.experiments.common import (
    INFRASTRUCTURE_RULE,
    PRICE_PER_MILLION,
    STALL_SECONDS,
    AcceptAll,
    Materials,
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
    run_team,
    start,
    timed,
    write,
)
from benchmarks.experiments.common import (
    seal as seal_study,
)
from benchmarks.experiments.provider_guard import AccountRefused, ProviderGuard
from benchmarks.experiments.rules import describe, relative_effect
from evals.audit_tasks import RULES
from evals.synthesis_tasks import GROUP, LABELS, SIZES, SYNTHESIS_RULES, SynthesisCase
from evals.synthesis_tasks import synthesis_cases as cases
from forgeharness.sdk import (
    AgentFinding,
    AgentReport,
    ApplicationSpec,
    AssessedAgentReport,
    CollaborationAgent,
    CollaborationEvidence,
    CollaborationPlan,
    CollaborationTask,
    EvidenceAssessment,
    FinalAction,
    Harness,
    Message,
    MessageRole,
    ModelRequest,
    ModelResult,
    ModelUsage,
    RunKernel,
    RunStatus,
    ToolCall,
    Usage,
    verify_trace,
)

ARMS = ("oneshot", "split-naive", "split-extract", "team-s1", "team-s2")
OUTPUT_TOKENS = 4096
SPLIT_IN_FLIGHT = 4
HTTP_TIMEOUT_SECONDS = 300.0
FLOOR, REDUCTION = 5, 0.5

LABEL_FORMAT = (
    "Return only raw JSON, without Markdown code fences: "
    '{"violations": ["svc-01", ...], "at_risk": ["svc-02", ...]} listing service IDs in '
    "ascending order. A service appears in at most one list; every service not listed is "
    "compliant. Use [] for an empty list."
)
EXTRACT_FORMAT = (
    "Each service lists the service IDs it currently calls under calls_upstream. "
    "Return only raw JSON, without Markdown code fences: "
    '{"violations": ["svc-01", ...], "calls_upstream": {"svc-01": ["svc-07"], ...}} where '
    "violations lists every service here that currently violates R1, R2 or R3, and "
    "calls_upstream maps every service here to the IDs in its calls_upstream line "
    "([] if it has none). Ignore calls mentioned only in the changelog."
)
SPECIALIST_S1 = (
    "You audit service configurations against R1-R3 only; you cannot see the other "
    "services, so never judge R4. For every assigned service add exactly one finding "
    "whose evidence_ids cite only that service and whose claim is either "
    "'violation R<n>; calls_upstream: [IDs]' or 'compliant; calls_upstream: [IDs]', "
    "copying the IDs from its calls_upstream line. Return only raw JSON without "
    "Markdown code fences."
)
COORDINATOR_S1 = (
    "You combine specialist audit reports; you see only those reports. Add one finding "
    "for every service that is violating or at risk, whose evidence_ids cite that "
    "service first. Its claim starts with 'violation' if a specialist found it "
    "violating; otherwise with 'at-risk' if any ID in its calls_upstream was found "
    "violating (also cite that upstream service). Add no findings for compliant "
    "services; if there are none to add, leave findings empty and put 'none' in "
    "unresolved. Return only raw JSON without Markdown code fences."
)
SPECIALIST_S2 = (
    "You audit service configurations against R1-R3 only; you cannot see the other "
    "services, so never use the verdict at-risk. Assess every assigned service as "
    "violation or compliant; in the reason name the rule broken, if any, and copy the "
    "IDs from its calls_upstream line as 'calls_upstream: [IDs]'. Return only raw JSON "
    "without Markdown code fences."
)
COORDINATOR_S2 = (
    "You combine specialist audit reports; you see only those reports. Assess every "
    "service: violation if a specialist assessed it as violation; otherwise at-risk if "
    "any ID in its calls_upstream was assessed as violation; otherwise compliant. In "
    "the reason name the rule or the violating upstream service. Return only raw JSON "
    "without Markdown code fences."
)

RULE = {
    "primary": "label errors: services whose predicted label differs from the expected "
    "label (violation, at-risk, compliant), summed over tasks, per arm",
    "secondary": "exact-label success per run; errors on hidden at-risk services; cost and "
    "latency are descriptive only",
    "failed_units": "a unit without a parseable answer labels nothing: its services count "
    "as compliant, and failed units are reported",
    "effect": describe(FLOOR, REDUCTION),
    "manipulation_check": "the task needs synthesis if oneshot is better than split-naive "
    "under the effect rule, pooled over all tasks",
    "comparisons": {
        "schema": "team-s2 against team-s1",
        "framework_vs_program": "team-s2 against split-extract",
        "framework_vs_single_call": "team-s2 against oneshot",
        "program_vs_single_call": "split-extract against oneshot",
    },
    "verdict": "collaboration earns its place on this task only if team-s2 is better than "
    "both split-extract and oneshot, pooled over all tasks",
    "infrastructure": INFRASTRUCTURE_RULE + " (per unit: a single call, a group or a plan)",
    "scope": "exploratory: one model, temperature 0, 6 tasks per N, one sample each",
}


def identity() -> dict[str, Any]:
    return {
        "files": file_sha256(
            "benchmarks/experiments/synthesis.py",
            "benchmarks/experiments/common.py",
            "benchmarks/experiments/rules.py",
            "evals/synthesis_tasks.py",
            "evals/audit_tasks.py",
            "src/forgeharness/sdk/collaboration.py",
        ),
        "output_tokens": OUTPUT_TOKENS,
        "group": GROUP,
        "split_in_flight": SPLIT_IN_FLIGHT,
    }


STUDY = Study(
    name="synthesis",
    identity=identity,
    cases=cases,
    evaluation_split="evaluation",
    output_tokens=OUTPUT_TOKENS,
)


# ------------------------------------------------------------------------ parsing


def _id_list(value: object) -> set[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return set(value)


def parse_labels(final: str | None, visible: tuple[str, ...]) -> dict[str, str] | None:
    """``{"violations", "at_risk"}`` from oneshot or one naive group; ``None`` if malformed."""
    try:
        value = json.loads(final or "")
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    violations, at_risk = _id_list(value.get("violations")), _id_list(value.get("at_risk"))
    if violations is None or at_risk is None:
        return None
    return {
        key: "violation" if key in violations else "at-risk" if key in at_risk else "compliant"
        for key in visible
    }


def parse_extract(
    final: str | None, visible: tuple[str, ...]
) -> tuple[set[str], dict[str, set[str]]] | None:
    """One extraction group's R1-R3 violators and calls; ``None`` if malformed."""
    try:
        value = json.loads(final or "")
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    violations, calls = _id_list(value.get("violations")), value.get("calls_upstream")
    if violations is None or not isinstance(calls, dict):
        return None
    parsed: dict[str, set[str]] = {}
    for key in visible:
        listed = _id_list(calls.get(key, []))
        if listed is None:
            return None
        parsed[key] = listed
    return violations & set(visible), parsed


def combine_extracts(
    parts: list[tuple[set[str], dict[str, set[str]]] | None], ids: tuple[str, ...]
) -> dict[str, str]:
    """Apply R4 in code over the united extractions; failed groups label nothing."""
    direct = {key for part in parts if part for key in part[0]}
    calls = {key: up for part in parts if part for key, up in part[1].items()}
    return {
        key: "violation"
        if key in direct
        else "at-risk"
        if calls.get(key, set()) & direct
        else "compliant"
        for key in ids
    }


_RANK = {"violation": 0, "at-risk": 1, "compliant": 2}


def parse_team(arm: str, final: str | None, ids: tuple[str, ...]) -> dict[str, str] | None:
    """The coordinator's labels; unlabeled services are compliant."""
    try:
        value = json.loads(final or "")
    except ValueError:
        return None
    labels = dict.fromkeys(ids, "compliant")
    try:
        if arm == "team-s2":
            assessed = AssessedAgentReport.model_validate(value)
            for key, assessment in assessed.assessments.items():
                if key in labels and assessment.verdict in _RANK:
                    labels[key] = assessment.verdict
            return labels
        report = AgentReport.model_validate(value)
    except ValueError:
        return None
    for finding in report.findings:
        words = finding.claim.strip().lower().replace(";", " ").replace(":", " ").split()
        label = words[0] if words else ""
        key = finding.evidence_ids[0]
        if label in _RANK and key in labels and _RANK[label] < _RANK[labels[key]]:
            labels[key] = label
    return labels


def grade(case: SynthesisCase, labels: dict[str, str]) -> dict[str, Any]:
    expected = case.labels
    wrong = sorted(key for key in expected if labels.get(key, "compliant") != expected[key])
    confusion = {
        f"{want}->{got}": sum(
            1 for key in expected if expected[key] == want and labels.get(key, "compliant") == got
        )
        for want in LABELS
        for got in LABELS
    }
    hidden = case.hidden_at_risk
    return {
        "wrong": wrong,
        "errors": len(wrong),
        "risk_errors": sum(
            1
            for key in wrong
            if expected[key] == "at-risk" or labels.get(key, "compliant") == "at-risk"
        ),
        "hidden_at_risk": len(hidden),
        "hidden_at_risk_missed": sum(labels.get(key) != "at-risk" for key in hidden),
        "confusion": confusion,
    }


# ------------------------------------------------------------------------ oracle


def _visible(request: ModelRequest) -> tuple[str, list[str]] | None:
    """The kind of unit a request belongs to and the service IDs it can see."""
    for message in request.messages:
        if message.role is not MessageRole.USER:
            continue
        try:
            value = json.loads(message.content or "")
        except ValueError:
            continue
        if isinstance(value, dict) and "collaboration_assignment" in value:
            task = value["collaboration_assignment"]
            if task == "synthesis":
                return "coordinator", []
            return "specialist:" + task, [item["id"] for item in value["evidence"]]
        if isinstance(value, dict) and "services" in value:
            return "reader", list(value["services"])
    return None


class SynthesisOracle:
    """Answers what each unit can see; a wiring check, never a measurement."""

    def __init__(self, case: SynthesisCase) -> None:
        self.case = case
        self.direct = set(case.violations)
        self.calls = case.calls

    def _labels(self, visible: list[str]) -> dict[str, str]:
        seen = set(visible)
        return {
            key: "violation"
            if key in self.direct
            else "at-risk"
            if set(self.calls[key]) & self.direct & seen
            else "compliant"
            for key in visible
        }

    async def decide(self, request: ModelRequest) -> ModelResult:
        usage = ModelUsage(input_tokens=10, output_tokens=5)
        unit = _visible(request)
        system = "\n".join(
            m.content or "" for m in request.messages if m.role is MessageRole.SYSTEM
        )
        if unit is None:
            raise ValueError("oracle could not identify the unit")
        kind, visible = unit
        assessed = "Assess every assigned evidence ID" in system
        if kind == "reader" and "calls_upstream maps every service" in system:
            content = json.dumps(
                {
                    "violations": sorted(self.direct & set(visible)),
                    "calls_upstream": {key: list(self.calls[key]) for key in visible},
                }
            )
        elif kind == "reader":
            labels = self._labels(visible)
            content = json.dumps(
                {
                    "violations": sorted(k for k, v in labels.items() if v == "violation"),
                    "at_risk": sorted(k for k, v in labels.items() if v == "at-risk"),
                }
            )
        else:
            task = "synthesis" if kind == "coordinator" else kind.split(":", 1)[1]
            ids = [s.id for s in self.case.services] if kind == "coordinator" else visible
            labels = (
                self.case.labels
                if kind == "coordinator"
                else {k: "violation" if k in self.direct else "compliant" for k in ids}
            )
            if assessed:
                report: Any = AssessedAgentReport(
                    task_id=task,
                    assessments={
                        key: EvidenceAssessment(verdict=labels[key], reason="oracle") for key in ids
                    },
                    findings=(),
                    summary="oracle",
                    unresolved=(),
                )
            else:
                # Specialists list every service; the coordinator only non-compliant
                # ones (schema 1 allows at most 30 findings).
                listed = [k for k in ids if kind != "coordinator" or labels[k] != "compliant"]
                report = AgentReport(
                    task_id=task,
                    summary="oracle",
                    findings=tuple(
                        AgentFinding(claim=f"{labels[key]}; oracle", evidence_ids=(key,))
                        for key in listed
                    ),
                    unresolved=() if listed else ("none",),
                )
            content = "```json\n" + report.model_dump_json() + "\n```"
        return ModelResult(action=FinalAction(content=content), usage=usage)


class SynthesisProvider(Provider):
    def inner(self, case: Any) -> Any:
        if self.args.mode == "contract":
            return SynthesisOracle(case)
        return super().inner(case)


# ------------------------------------------------------------------------ units


class Reader:
    """Read the unit's services deterministically, then make exactly one model call."""

    def __init__(self, keys: list[str], system: str) -> None:
        self.keys, self.system = keys, system

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
                Message(role=MessageRole.SYSTEM, content=self.system),
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


def team_plan(case: SynthesisCase, arm: str, model_identity: str) -> CollaborationPlan:
    """One specialist per group of 8 and a coordinator; schema 2 for ``team-s2``."""
    assessed = arm == "team-s2"
    specialist = Usage(steps=1, input_tokens=16_000, output_tokens=OUTPUT_TOKENS)
    return CollaborationPlan(
        version=f"synthesis-{arm}-{case.id}",
        model_identity=model_identity,
        goal=SYNTHESIS_RULES,
        agents=(
            CollaborationAgent(
                id="auditor",
                version="1",
                instructions=SPECIALIST_S2 if assessed else SPECIALIST_S1,
                max_usage=specialist,
            ),
            CollaborationAgent(
                id="coordinator",
                version="1",
                instructions=SYNTHESIS_RULES
                + "\n"
                + (COORDINATOR_S2 if assessed else COORDINATOR_S1),
                max_usage=specialist,
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
                objective="Audit the assigned services against R1-R3.",
                evidence_ids=group,
            )
            for number, group in enumerate(case.groups())
        ),
        coordinator_id="coordinator",
        assessment_verdicts=LABELS if assessed else None,
    )


async def run_unit(
    case: SynthesisCase,
    ids: tuple[str, ...],
    kind: str,
    args: Any,
    destination: Path,
    *,
    guard: ProviderGuard,
) -> dict[str, Any]:
    """One reader call over ``ids`` (``kind`` = labels or extract) or one team plan."""
    provider = SynthesisProvider(args, guard, OUTPUT_TOKENS, timeout=HTTP_TIMEOUT_SECONDS)
    run_id = f"{case.id}-{kind}-{ids[0]}"
    elapsed = timed()
    status, error, trace_valid = "error", None, False
    parsed: Any = None
    try:
        if kind.startswith("team"):
            plan = team_plan(case, kind, f"{args.model}:t0:max{OUTPUT_TOKENS}")
            status, error, final, trace = await run_team(
                plan, lambda: provider.model(case), destination, run_id, OUTPUT_TOKENS
            )
        else:
            state = destination / "state"
            state.mkdir(parents=True, mode=0o700)
            unit = replace(
                case, services=tuple(s for s in case.services if s.id in ids), size=len(ids)
            )
            tool = Materials(unit)
            system = (
                RULES + "\n" + EXTRACT_FORMAT
                if kind == "extract"
                else SYNTHESIS_RULES + "\n" + LABEL_FORMAT
            )
            spec = ApplicationSpec(
                id="synthesis-study",
                version="1",
                policy_version="1",
                model_factory=lambda: provider.model(case),
                tools=(tool,),
                policy=ReadOnly(),
                verifier=AcceptAll(),
                budget=oneshot_budget(len(ids), OUTPUT_TOKENS),
                instructions=(system,),
            )
            harness = Harness(spec, state_dir=state, orchestrator=Reader(list(ids), system))
            context = context_for(spec.id, "1", "1", run_id)
            task = "Service IDs: " + ", ".join(ids)
            result = await harness.run(
                context=context, task=task + "\nRead each service with read_material."
            )
            status, error, final = result.status.value, result.error, result.final_output
            trace = harness.trace_path(context)
        trace_valid = verify_trace(trace).valid
        if status == "succeeded":
            if kind.startswith("team"):
                parsed = parse_team(kind, final, ids)
            elif kind == "extract":
                extracted = parse_extract(final, ids)
                parsed = (
                    None
                    if extracted is None
                    else {
                        "violations": sorted(extracted[0]),
                        "calls_upstream": {k: sorted(v) for k, v in extracted[1].items()},
                    }
                )
            else:
                parsed = parse_labels(final, ids)
    except Exception as exc:
        # A bounded exception class only: provider bodies and credentials stay out.
        error = type(exc).__name__
    finally:
        await provider.close()
    return {
        "unit": f"{case.id}:{ids[0]}..{ids[-1]}",
        "kind": kind,
        "materials": len(ids),
        "status": status,
        "error": error,
        "parsed": parsed,
        "trace_valid": trace_valid,
        "seconds": elapsed(),
        **provider.metrics(),
    }


async def run_arm(
    case: SynthesisCase, arm: str, args: Any, destination: Path, *, guard: ProviderGuard
) -> dict[str, Any]:
    ids = tuple(service.id for service in case.services)
    if arm == "oneshot":
        units, kind = [ids], "labels"
    elif arm == "split-naive":
        units, kind = case.groups(), "labels"
    elif arm == "split-extract":
        units, kind = case.groups(), "extract"
    else:
        units, kind = [ids], arm
    gate = asyncio.Semaphore(SPLIT_IN_FLIGHT)

    async def one(unit: tuple[str, ...]) -> dict[str, Any]:
        async with gate:
            guard.check()
            return await measured(
                partial(run_unit, case, unit, kind, args, guard=guard),
                destination / "units" / unit[0],
            )

    elapsed = timed()
    unit_rows = await asyncio.gather(*(one(unit) for unit in units))
    seconds = elapsed()
    failed = [row["unit"] for row in unit_rows if row["parsed"] is None]
    if kind == "extract":
        labels = combine_extracts(
            [
                None
                if row["parsed"] is None
                else (
                    set(row["parsed"]["violations"]),
                    {k: set(v) for k, v in row["parsed"]["calls_upstream"].items()},
                )
                for row in unit_rows
            ],
            ids,
        )
    else:
        labels = dict.fromkeys(ids, "compliant")
        for row in unit_rows:
            labels.update(row["parsed"] or {})
    graded = grade(case, labels)
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
        "labels": labels,
        **graded,
        "judgements": case.size,
        "success": graded["errors"] == 0 and not failed and trace_valid,
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
        "stalled_calls": sum(s > STALL_SECONDS for s in calls),
        "infrastructure_error": any(row["infrastructure_error"] for row in unit_rows),
        "infrastructure_retries": sum(len(row["discarded_attempts"]) for row in unit_rows),
        "unit_rows": [{k: v for k, v in u.items() if k != "call_seconds"} for u in unit_rows],
    }
    write(destination / "result.json", row)
    return row


# ------------------------------------------------------------------------ summary


def judge(arms: dict[str, Any]) -> dict[str, Any]:
    """Apply RULE to one pool of tasks."""
    if not set(ARMS) <= set(arms):
        return {"incomplete": True}

    def errors(arm: str) -> int:
        return int(arms[arm]["errors"])

    def effect(baseline: str, candidate: str) -> str:
        return relative_effect(
            errors(baseline), errors(candidate), floor=FLOOR, reduction=REDUCTION
        )

    comparisons = {
        "schema": effect("team-s1", "team-s2"),
        "framework_vs_program": effect("split-extract", "team-s2"),
        "framework_vs_single_call": effect("oneshot", "team-s2"),
        "program_vs_single_call": effect("oneshot", "split-extract"),
    }
    return {
        "task_needs_synthesis": effect("split-naive", "oneshot") == "better",
        "comparisons": comparisons,
        "collaboration_earns_its_place": comparisons["framework_vs_program"] == "better"
        and comparisons["framework_vs_single_call"] == "better",
    }


def _arm_summary(selected: list[dict[str, Any]]) -> dict[str, Any]:
    judgements = sum(r["judgements"] for r in selected)
    errors = sum(r["errors"] for r in selected)
    return {
        "runs": len(selected),
        "successes": sum(r["success"] for r in selected),
        "judgements": judgements,
        "errors": errors,
        "error_rate": errors / judgements,
        "risk_errors": sum(r["risk_errors"] for r in selected),
        "hidden_at_risk": sum(r["hidden_at_risk"] for r in selected),
        "hidden_at_risk_missed": sum(r["hidden_at_risk_missed"] for r in selected),
        "errors_by_case": {r["case"]: r["errors"] for r in selected},
        "failed_units": sum(len(r["failed_units"]) for r in selected),
        "latency_p50_seconds": percentile([r["seconds"] for r in selected], 0.5),
        "mean_model_calls": statistics.mean(r["model_calls"] for r in selected),
        "cost_usd_peak_per_run": statistics.mean(r["cost_usd_peak"] for r in selected),
        "fences_stripped": sum(r["fences_stripped"] for r in selected),
        "infrastructure_retries": sum(r["infrastructure_retries"] for r in selected),
    }


def summarize(rows: list[dict[str, Any]], expected_runs: int, mode: str) -> dict[str, Any]:
    pools: dict[str, Any] = {}
    for label, sizes in [*((str(size), (size,)) for size in SIZES), ("pooled", SIZES)]:
        arms = {
            arm: _arm_summary(selected)
            for arm in ARMS
            if (selected := [r for r in rows if r["size"] in sizes and r["arm"] == arm])
        }
        pools[label] = {"arms": arms, "judgement": judge(arms)}
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
        "by_size": pools,
        "price_basis": {"usd_per_million_tokens_peak": PRICE_PER_MILLION},
        "limits": [
            "synthetic author-generated tasks; one-hop dependency risk only",
            "one model at temperature 0; 6 tasks per N, one sample each",
            "groups of 8 for every split and team arm; no other grouping tested",
        ],
    }


async def execute(args: Any) -> dict[str, Any]:
    if args.split == STUDY.evaluation_split and (args.arms or args.sizes):
        raise ValueError("the evaluation split runs every sealed arm and size")
    manifest, _ = start(STUDY, args)
    selected = cases(args.split)
    if args.sizes:
        selected = tuple(c for c in selected if c.size in args.sizes)
    arms = tuple(args.arms) if args.arms else ARMS
    expected_runs = len(selected) * len(arms)
    manifest |= {"cases": [c.id for c in selected], "arms": list(arms)}
    write(args.output / "manifest.json", manifest)
    guard = ProviderGuard()
    rows: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(selected):
            # Rotate arm order so no arm always runs first against a warm cache.
            offset = index % len(arms)
            for arm in (*arms[offset:], *arms[:offset]):
                guard.check()
                row = await run_arm(case, arm, args, args.output / case.id / arm, guard=guard)
                rows.append(row)
                print(
                    f"{len(rows)}/{expected_runs} {case.id} {arm:<13} "
                    f"errors={row['errors']}/{row['judgements']} "
                    f"hidden_missed={row['hidden_at_risk_missed']}/{row['hidden_at_risk']} "
                    f"failed_units={len(row['failed_units'])} {row['seconds']:.1f}s",
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
        if set(summary["by_size"]["pooled"]["arms"]) != set(ARMS):
            raise ValueError("the dev run must cover every arm")

    seal_study(
        STUDY,
        args,
        {
            "evaluation_cases": [c.id for c in cases("evaluation")],
            "planned_runs": len(cases("evaluation")) * len(ARMS),
            "rule": RULE,
        },
        checks,
    )


def main() -> None:
    def configure(parser: Any) -> None:
        parser.add_argument("--sizes", type=int, nargs="*", choices=SIZES)
        parser.add_argument("--arms", nargs="*", choices=ARMS)

    cli(STUDY, __doc__ or "", execute, seal, configure)


if __name__ == "__main__":
    main()

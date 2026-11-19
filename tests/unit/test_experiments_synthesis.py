"""The cross-material synthesis study: tasks, parsing, grading and the sealed rule."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from test_audit_tasks import _rule_check

import benchmarks.experiments.common as common
import benchmarks.experiments.synthesis as study
from benchmarks.experiments.provider_guard import ProviderGuard
from evals.audit_tasks import RULES, ablation_cases, cases, long_cases
from evals.synthesis_tasks import GROUP, LABELS, SIZES, SYNTHESIS_RULES, synthesis_cases
from forgeharness.sdk import CredentialMask, collaboration_graph

# Pinned so a generator change cannot silently alter a sealed corpus.
CORPUS_SHA256 = {
    "dev": "1fcdb6339c818fa2b69697ed9c026bf70b3acfbc35ed91162906d73dd1e0b47b",
    "evaluation": "4d7fd65ea4f2967e926acc2e855fe5493b2b600e1a4c57fea93322867e087365",
}


def _corpus_digest(split: str) -> str:
    payload = [
        [case.id, [s.text for s in case.services], case.upstream, case.labels]
        for case in synthesis_cases(split)
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _current_calls(text: str) -> tuple[str, ...]:
    match = re.search(r"\ncalls_upstream: \[(.*)\]\n", text)
    assert match is not None
    return tuple(item for item in match.group(1).split(", ") if item)


@pytest.mark.parametrize("split", ["dev", "evaluation"])
def test_corpus_is_pinned(split: str) -> None:
    assert _corpus_digest(split) == CORPUS_SHA256[split]


def test_tasks_are_deterministic_fresh_and_labelled_from_the_materials() -> None:
    dev, evaluation = synthesis_cases("dev"), synthesis_cases("evaluation")
    assert dev == synthesis_cases("dev") and len(dev) == 2
    assert len(evaluation) == 12 and sorted({c.size for c in evaluation}) == list(SIZES)
    earlier = {
        s.text
        for c in (*cases("dev"), *cases("evaluation"), *ablation_cases(), *long_cases("dev"))
        for s in c.services
    }
    mask = CredentialMask()
    for case in (*dev, *evaluation):
        assert len({s.id for s in case.services}) == case.size
        direct = {s.id for s in case.services if _rule_check(s) is not None}
        assert direct == set(case.violations)
        for service in case.services:
            assert _rule_check(service) == service.violation
            assert service.text not in earlier
            assert "\ndependencies: " not in service.text
            calls = _current_calls(service.text)
            assert calls == case.calls[service.id] and service.id not in calls
            removed = re.findall(r"removed synchronous call to (svc-\d+)", service.text)
            # The near miss is history only: never a current call.
            assert all(key not in calls and key in direct for key in removed)
            assert mask.transform(service.text) == service.text
        # The answer key follows from the materials alone.
        for key, label in case.labels.items():
            if key in direct:
                assert label == "violation"
            else:
                expected = "at-risk" if set(case.calls[key]) & direct else "compliant"
                assert label == expected


def test_most_at_risk_services_cross_a_group_boundary() -> None:
    evaluation = synthesis_cases("evaluation")
    at_risk = sum(len(c.at_risk) for c in evaluation)
    hidden = sum(len(c.hidden_at_risk) for c in evaluation)
    assert at_risk >= 40 and hidden / at_risk >= 0.5
    for case in evaluation:
        assert set(case.hidden_at_risk) <= set(case.at_risk)
        assert [len(g) for g in case.groups()] == [GROUP] * (case.size // GROUP)


def test_rules_extend_the_audit_rules_with_r4() -> None:
    assert SYNTHESIS_RULES.startswith(RULES.split("Identify every service")[0])
    assert "R4 Dependency risk" in SYNTHESIS_RULES
    assert "Identify every service that currently violates" not in SYNTHESIS_RULES


def test_parsers_reject_malformed_answers_and_apply_precedence() -> None:
    ids = ("svc-01", "svc-02", "svc-03")
    labels = study.parse_labels('{"violations": ["svc-01"], "at_risk": ["svc-01", "svc-02"]}', ids)
    assert labels == {"svc-01": "violation", "svc-02": "at-risk", "svc-03": "compliant"}
    for bad in ("not json", "[]", '{"violations": "svc-01", "at_risk": []}', '{"violations": []}'):
        assert study.parse_labels(bad, ids) is None
    extract = study.parse_extract(
        '{"violations": ["svc-01", "svc-09"], "calls_upstream": {"svc-02": ["svc-01"]}}', ids
    )
    assert extract == ({"svc-01"}, {"svc-01": set(), "svc-02": {"svc-01"}, "svc-03": set()})
    assert study.parse_extract('{"violations": [], "calls_upstream": {"svc-01": 3}}', ids) is None
    combined = study.combine_extracts(
        [({"svc-01"}, {"svc-01": set(), "svc-02": set()}), ({"svc-03"}, {"svc-04": {"svc-01"}})],
        ("svc-01", "svc-02", "svc-03", "svc-04"),
    )
    assert combined == {
        "svc-01": "violation",
        "svc-02": "compliant",
        "svc-03": "violation",
        "svc-04": "at-risk",
    }
    # A failed group labels nothing, so its services count as compliant.
    assert study.combine_extracts([None], ("svc-01",)) == {"svc-01": "compliant"}


def test_team_parsers_read_schema_1_claims_and_schema_2_verdicts() -> None:
    ids = ("svc-01", "svc-02", "svc-03")
    schema1 = {
        "schema_version": 1,
        "task_id": "synthesis",
        "summary": "s",
        "findings": [
            {"claim": "At-risk; calls svc-01", "evidence_ids": ["svc-02", "svc-01"]},
            {"claim": "violation R2", "evidence_ids": ["svc-01"]},
            {"claim": "compliant", "evidence_ids": ["svc-02"]},
        ],
        "unresolved": [],
    }
    assert study.parse_team("team-s1", json.dumps(schema1), ids) == {
        "svc-01": "violation",
        "svc-02": "at-risk",
        "svc-03": "compliant",
    }
    schema2 = {
        "schema_version": 2,
        "task_id": "synthesis",
        "assessments": {key: {"verdict": "at-risk", "reason": "r"} for key in ids},
        "findings": [],
        "summary": "s",
        "unresolved": [],
    }
    assert study.parse_team("team-s2", json.dumps(schema2), ids) == dict.fromkeys(ids, "at-risk")
    assert study.parse_team("team-s2", json.dumps(schema1), ids) is None
    assert study.parse_team("team-s1", "not json", ids) is None


def test_grading_counts_label_errors_and_hidden_misses() -> None:
    case = synthesis_cases("dev")[0]
    perfect = study.grade(case, case.labels)
    assert perfect["errors"] == 0 and perfect["hidden_at_risk_missed"] == 0
    assert sum(perfect["confusion"].values()) == case.size
    blind = dict(case.labels)
    for key in case.hidden_at_risk:
        blind[key] = "compliant"
    graded = study.grade(case, blind)
    assert graded["errors"] == graded["risk_errors"] == len(case.hidden_at_risk) > 0
    assert graded["hidden_at_risk_missed"] == len(case.hidden_at_risk)
    assert graded["confusion"]["at-risk->compliant"] == len(case.hidden_at_risk)


def test_team_plans_use_groups_of_eight_and_schema_2_only_for_team_s2() -> None:
    case = synthesis_cases("dev")[1]
    s1 = study.team_plan(case, "team-s1", "m")
    s2 = study.team_plan(case, "team-s2", "m")
    assert s1.assessment_verdicts is None and s2.assessment_verdicts == LABELS
    assert [t.evidence_ids for t in s2.tasks] == case.groups()
    coordinator = next(n for n in collaboration_graph(s2).nodes if n.id == "coordinator")
    assert coordinator.output_schema["properties"]["assessments"]["required"] == [
        s.id for s in case.services
    ]
    assert common.team_budget(s2, study.OUTPUT_TOKENS).max_steps == len(case.groups()) + 1
    # Schema 1 caps findings at 30, so its coordinator lists only non-compliant services.
    assert max(len(c.violations) + len(c.at_risk) for c in synthesis_cases("evaluation")) <= 30


def _arm(errors: int) -> dict[str, Any]:
    return {"errors": errors}


def test_the_pre_registered_rule() -> None:
    verdict = study.judge(
        {
            "oneshot": _arm(20),
            "split-naive": _arm(45),
            "split-extract": _arm(18),
            "team-s1": _arm(16),
            "team-s2": _arm(6),
        }
    )
    assert verdict == {
        "task_needs_synthesis": True,
        "comparisons": {
            "schema": "better",
            "framework_vs_program": "better",
            "framework_vs_single_call": "better",
            "program_vs_single_call": "no_clear_difference",
        },
        "collaboration_earns_its_place": True,
    }
    quiet = study.judge({arm: _arm(2) for arm in study.ARMS})
    assert quiet["task_needs_synthesis"] is False
    assert set(quiet["comparisons"].values()) == {"insufficient_errors"}
    assert quiet["collaboration_earns_its_place"] is False
    assert study.judge({"oneshot": _arm(1)}) == {"incomplete": True}


def _args(output: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "split": "dev",
        "mode": "contract",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "sizes": [16],
        "arms": None,
        "output": output,
        "protocol": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


async def test_contract_run_wires_every_arm_and_naive_split_loses_hidden_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, "REPORTS", tmp_path)
    summary = await study.execute(_args(tmp_path / "dev"))
    case = synthesis_cases("dev")[0]
    arms = summary["by_size"]["16"]["arms"]
    assert summary["complete"] is True and summary["measurement"] is False
    expected = dict.fromkeys(study.ARMS, 0) | {"split-naive": len(case.hidden_at_risk)}
    assert {arm: data["errors"] for arm, data in arms.items()} == expected
    assert arms["split-naive"]["hidden_at_risk_missed"] == len(case.hidden_at_risk)
    rows = {
        json.loads(p.read_text())["arm"]: json.loads(p.read_text())
        for p in (tmp_path / "dev").glob("*/*/result.json")
    }
    assert {arm: row["units"] for arm, row in rows.items()} == {
        "oneshot": 1, "split-naive": 2, "split-extract": 2, "team-s1": 1, "team-s2": 1
    }  # fmt: skip
    assert all(row["trace_valid"] and not row["failed_units"] for row in rows.values())
    assert arms["team-s2"]["fences_stripped"] > 0
    manifest = json.loads((tmp_path / "dev" / "manifest.json").read_text())
    assert "api_key" not in manifest["model_parameters"]
    with pytest.raises(ValueError, match="complete live dev run"):
        study.seal(argparse.Namespace(dev_report=tmp_path / "dev", protocol=tmp_path / "p.json"))


async def test_a_failed_extraction_group_labels_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = synthesis_cases("dev")[0]
    first = case.groups()[0]

    async def fake(attempt, destination):  # type: ignore[no-untyped-def]
        ids = attempt.args[1]
        calls = case.calls
        parsed = (
            None
            if ids == first
            else {
                "violations": sorted(set(case.violations) & set(ids)),
                "calls_upstream": {k: list(calls[k]) for k in ids},
            }
        )
        return {
            "unit": ids[0],
            "parsed": parsed,
            "trace_valid": True,
            "model_calls": 1,
            "prompt_tokens": 1,
            "cached_prompt_tokens": 0,
            "completion_tokens": 1,
            "max_prompt_tokens": 1,
            "fences_stripped": 0,
            "usage_known": True,
            "call_seconds": [0.1],
            "infrastructure_error": False,
            "discarded_attempts": [],
        }

    monkeypatch.setattr(study, "measured", fake)
    row = await study.run_arm(
        case, "split-extract", _args(tmp_path), tmp_path / "r", guard=ProviderGuard()
    )
    assert row["failed_units"] == [first[0]] and row["success"] is False
    assert all(row["labels"][key] != "violation" for key in first)
    assert row["errors"] > 0


async def test_the_evaluation_split_refuses_subsets_and_needs_a_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, "REPORTS", tmp_path)
    with pytest.raises(ValueError, match="every sealed arm"):
        await study.execute(_args(tmp_path / "a", split="evaluation", arms=["oneshot"]))
    monkeypatch.setenv("FORGE_API_KEY", "not-used")
    with pytest.raises(ValueError, match="sealed protocol"):
        await study.execute(_args(tmp_path / "b", mode="live", split="evaluation", sizes=None))


def test_sealing_needs_a_clean_dev_run_over_every_arm(tmp_path: Path) -> None:
    def dev(name: str, arms: tuple[str, ...] = study.ARMS, **summary: object) -> Path:
        folder = tmp_path / name
        common.write(
            folder / "manifest.json",
            {"mode": "live", "split": "dev", "identity": study.identity(), "model": "m"},
        )
        common.write(
            folder / "summary.json",
            {
                "complete": True,
                "measurement": True,
                "failed_units": 0,
                "by_size": {"pooled": {"arms": {key: {} for key in arms}}},
                **summary,
            },
        )
        return folder

    def seal(folder: Path, protocol: str) -> None:
        study.seal(argparse.Namespace(dev_report=folder, protocol=tmp_path / protocol))

    with pytest.raises(ValueError, match="no parseable answer"):
        seal(dev("failed", failed_units=1), "p1.json")
    with pytest.raises(ValueError, match="every arm"):
        seal(dev("partial", arms=("oneshot",)), "p2.json")
    seal(dev("clean"), "p3.json")
    protocol = json.loads((tmp_path / "p3.json").read_text())
    assert protocol["planned_runs"] == 60 and protocol["rule"] == study.RULE
    assert protocol["identity"]["files"]["src/forgeharness/sdk/collaboration.py"]

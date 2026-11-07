"""The long-context scaling study: large-N tasks, unit-level fan-out and the sealed rule."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from test_multi_material_cases import _rule_check

import benchmarks.long_context_scaling as study
from evals.long_context_cases import SIZES, long_cases
from evals.multi_material_ablation_cases import ablation_cases
from evals.multi_material_cases import cases
from forgeharness.sdk import CredentialMask


def test_large_tasks_are_deterministic_fresh_labelled_and_mask_safe() -> None:
    dev, evaluation = long_cases("dev"), long_cases("evaluation")
    assert dev == long_cases("dev") and evaluation == long_cases("evaluation")
    assert sorted({c.size for c in evaluation}) == list(SIZES) and len(evaluation) == 16
    earlier = {
        s.text for c in (*cases("dev"), *cases("evaluation"), *ablation_cases()) for s in c.services
    }
    seen: set[str] = set()
    mask = CredentialMask()
    for case in (*dev, *evaluation):
        ids = [s.id for s in case.services]
        assert len(set(ids)) == case.size and 1 <= len(case.violations) < case.size
        names = {s.text.split("\n", 1)[0].split("(")[1] for s in case.services}
        assert len(names) == case.size
        for service in case.services:
            assert _rule_check(service) == service.violation, (case.id, service.id)
            assert service.text not in earlier and service.text not in seen
            seen.add(service.text)
        assert all(mask.transform(s.text) == s.text for s in case.services[:64])


def test_groups_are_contiguous_and_keep_service_ids() -> None:
    case = long_cases("dev")[1]
    assert case.size == 128
    parts = study.groups(case, study.TEAM_GROUP)
    assert len(parts) == 8 and {p.size for p in parts} == {16}
    assert tuple(s for p in parts for s in p.services) == case.services
    assert [p.id for p in parts][:2] == [f"{case.id}-g01", f"{case.id}-g02"]


def test_the_team_unit_is_eight_specialists_of_two_within_the_framework_caps() -> None:
    unit = study.groups(long_cases("dev")[0], study.TEAM_GROUP)[0]
    plan = study.team_plan(unit, "m")
    assert len(plan.tasks) == 8 and {len(t.evidence_ids) for t in plan.tasks} == {2}
    assert all(agent.max_usage.output_tokens == study.OUTPUT_TOKENS for agent in plan.agents)
    assert study.team_budget(plan).max_steps == 9


def _arm(errors: int, judgements: int) -> dict[str, Any]:
    return {"errors": errors, "error_rate": errors / judgements}


def test_the_pre_registered_rule() -> None:
    verdict = study.judge(
        {"oneshot": _arm(40, 4096), "split-32": _arm(12, 4096), "team-16": _arm(5, 4096)}, 1024
    )
    assert verdict == {
        "oneshot_degrades": True,
        "fanout": {"split-32": "worth_it", "team-16": "worth_it"},
        "framework_vs_program": "framework_better",
    }
    # Ten errors over 4096 judgements is 0.24%: a fan-out case, but not degradation.
    quiet = study.judge(
        {"oneshot": _arm(10, 4096), "split-32": _arm(6, 4096), "team-16": _arm(12, 4096)}, 1024
    )
    assert quiet["oneshot_degrades"] is False
    assert quiet["fanout"] == {"split-32": "not_worth_it", "team-16": "not_worth_it"}
    assert quiet["framework_vs_program"] == "program_better"
    small = study.judge(
        {"oneshot": _arm(3, 128), "split-32": _arm(0, 128), "team-16": _arm(4, 128)}, 32
    )
    assert small["oneshot_degrades"] is False
    assert small["fanout"]["split-32"] == "no_quality_case"
    assert small["framework_vs_program"] == "no_clear_difference"


def _args(output: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "split": "dev",
        "mode": "contract",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "sizes": [32],
        "output": output,
        "protocol": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


async def test_contract_run_wires_every_arm_and_is_never_a_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(study, "REPORTS", tmp_path)
    summary = await study.execute(_args(tmp_path / "dev"))
    arms = summary["by_size"]["32"]["arms"]
    assert summary["complete"] is True and summary["measurement"] is False
    assert {arm: data["errors"] for arm, data in arms.items()} == dict.fromkeys(study.ARMS, 0)
    rows = {
        json.loads(p.read_text())["arm"]: json.loads(p.read_text())
        for p in (tmp_path / "dev").glob("*/*/result.json")
    }
    assert {arm: row["units"] for arm, row in rows.items()} == {
        "oneshot": 1, "split-32": 1, "team-16": 2
    }  # fmt: skip
    assert all(row["success"] and row["trace_valid"] for row in rows.values())
    assert arms["team-16"]["fences_stripped"] > 0
    manifest = json.loads((tmp_path / "dev" / "manifest.json").read_text())
    assert "api_key" not in manifest["model_parameters"]
    with pytest.raises(ValueError, match="complete live dev run"):
        study.seal(argparse.Namespace(dev_report=tmp_path / "dev", protocol=tmp_path / "p.json"))


def _unit(claimed: list[str] | None, **fields: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "unit": "u",
        "claimed": claimed,
        "trace_valid": True,
        "model_calls": 1,
        "prompt_tokens": 10,
        "cached_prompt_tokens": 0,
        "completion_tokens": 2,
        "max_prompt_tokens": 10,
        "fences_stripped": 0,
        "usage_known": True,
        "call_seconds": [0.5],
        "infrastructure_error": False,
        "infrastructure_retryable": False,
        "error": None,
        "http_statuses": [200],
        "missing_responses": 0,
        "seconds": 0.5,
    }
    row.update(fields)
    return row


async def test_a_failed_unit_claims_nothing_and_its_violators_count_as_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = long_cases("dev")[1]
    parts = study.groups(case, study.SPLIT_GROUP)

    async def fake(unit, kind, args, destination):  # type: ignore[no-untyped-def]
        if unit.id == parts[0].id:
            return {**_unit(None, unit=unit.id), "attempt": 0, "discarded_attempts": []}
        return {
            **_unit(list(unit.violations), unit=unit.id),
            "attempt": 0,
            "discarded_attempts": [],
        }

    monkeypatch.setattr(study, "run_unit_measured", fake)
    row = await study.run_arm(case, "split-32", _args(tmp_path), tmp_path / "run")
    assert row["failed_units"] == [parts[0].id]
    assert row["misses"] == sorted(parts[0].violations) and row["false_alarms"] == []
    assert row["errors"] == len(parts[0].violations) and row["success"] is False


async def test_only_the_unit_with_provider_trouble_is_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        _unit(None, infrastructure_error=True, infrastructure_retryable=True, http_statuses=[503]),
        _unit(["svc-01"]),
    ]
    seen: list[Path] = []

    async def fake(unit, kind, args, destination):  # type: ignore[no-untyped-def]
        seen.append(destination)
        return dict(script.pop(0))

    monkeypatch.setattr(study, "run_unit", fake)
    monkeypatch.setattr(study, "INFRA_BACKOFF_SECONDS", (0.0,))
    unit = long_cases("dev")[0]
    row = await study.run_unit_measured(unit, "oneshot", None, tmp_path / "u")
    assert row["attempt"] == 1 and row["claimed"] == ["svc-01"]
    assert [d["http_statuses"] for d in row["discarded_attempts"]] == [[503]]
    assert seen == [tmp_path / "u" / "attempt-0", tmp_path / "u" / "attempt-1"]


async def test_the_evaluation_split_needs_a_sealed_protocol_in_live_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(study, "REPORTS", tmp_path)
    monkeypatch.setenv("FORGE_API_KEY", "not-used")
    with pytest.raises(ValueError, match="sealed protocol"):
        await study.execute(_args(tmp_path / "run", mode="live", split="evaluation"))


def test_sealing_needs_a_clean_dev_run_where_every_unit_answered(tmp_path: Path) -> None:
    def dev(name: str, **summary: object) -> Path:
        folder = tmp_path / name
        study.write(
            folder / "manifest.json",
            {"mode": "live", "split": "dev", "identity": study.identity(), "model": "m"},
        )
        study.write(
            folder / "summary.json",
            {"complete": True, "measurement": True, "failed_units": 0, **summary},
        )
        return folder

    def seal(folder: Path, protocol: str) -> None:
        study.seal(argparse.Namespace(dev_report=folder, protocol=tmp_path / protocol))

    with pytest.raises(ValueError, match="not a clean measurement"):
        seal(dev("infra", measurement=False), "p1.json")
    with pytest.raises(ValueError, match="no parseable answer"):
        seal(dev("failed", failed_units=1), "p2.json")
    seal(dev("clean"), "p3.json")
    protocol = json.loads((tmp_path / "p3.json").read_text())
    assert protocol["planned_runs"] == 48 and protocol["rule"] == study.RULE
    with pytest.raises(ValueError, match="immutable"):
        seal(dev("again"), "p3.json")

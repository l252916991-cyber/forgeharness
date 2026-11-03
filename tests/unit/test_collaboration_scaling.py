"""Wiring and grading of the collaboration scaling study, without any model service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import benchmarks.collaboration_scaling as study


def _args(output: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "split": "dev",
        "mode": "contract",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "samples": 1,
        "sizes": [4],
        "output": output,
        "protocol": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_grading_is_an_exact_set_match_with_item_level_scores() -> None:
    expected = ("svc-02", "svc-05")
    assert study.grade(expected, ["svc-02", "svc-05"])["success"] is True
    partial = study.grade(expected, ["svc-02", "svc-03"])
    assert partial["success"] is False
    assert partial["precision"] == partial["recall"] == pytest.approx(0.5)
    assert study.grade(expected, None) == {
        "success": False, "precision": 0.0, "recall": 0.0, "f1": 0.0
    }  # fmt: skip


def test_each_arm_answer_is_parsed_from_its_own_format() -> None:
    single = json.dumps({"violations": ["svc-03", "svc-01", "svc-01"]})
    assert study.predicted("react", single) == ["svc-01", "svc-03"]
    report = study.AgentReport(
        task_id="synthesis",
        summary="s",
        findings=(
            study.AgentFinding(claim="R1", evidence_ids=("svc-04",)),
            study.AgentFinding(claim="R2", evidence_ids=("svc-02",)),
        ),
        unresolved=(),
    )
    assert study.predicted("team", report.model_dump_json()) == ["svc-02", "svc-04"]
    assert study.predicted("oneshot", "not json") is None
    assert study.predicted("react", json.dumps({"violations": "svc-01"})) is None
    assert study.unfence('```json\n{"a": 1}\n```') == ('{"a": 1}', True)
    assert study.unfence('{"a": 1}') == ('{"a": 1}', False)


def _arm(success: float, latency: float, cost: float) -> dict[str, float]:
    return {
        "success_rate": success,
        "latency_p50_seconds": latency,
        "cost_usd_peak_per_run": cost,
    }


def test_the_pre_registered_decision_rule() -> None:
    react, oneshot = _arm(0.5, 40, 0.02), _arm(0.75, 10, 0.01)
    assert study.decide({"react": react, "oneshot": oneshot, "team": _arm(1.0, 30, 0.05)}) == (
        "team_worth_it:quality"
    )
    assert study.decide({"react": react, "oneshot": oneshot, "team": _arm(0.75, 7, 0.02)}) == (
        "team_worth_it:efficiency"
    )
    assert study.decide({"react": react, "oneshot": oneshot, "team": _arm(0.75, 9, 0.02)}) == (
        "team_not_worth_it"
    )
    assert study.decide({"react": react, "team": _arm(1.0, 1, 0.01)}) == "incomplete"


async def test_contract_run_wires_every_arm_and_is_never_a_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(study, "REPORTS", tmp_path)
    summary = await study.execute(_args(tmp_path / "run"))
    arms = summary["by_size"]["4"]["arms"]
    assert summary["complete"] is True and summary["measurement"] is False
    assert {arm: data["successes"] for arm, data in arms.items()} == {
        "react": 1, "oneshot": 1, "team": 1
    }  # fmt: skip
    # The oracle fences its specialist reports; the shared normalizer must catch them.
    assert arms["team"]["fences_stripped"] > 0
    rows = [json.loads(p.read_text()) for p in (tmp_path / "run").glob("*/*/result.json")]
    assert len(rows) == 3 and all(row["trace_valid"] for row in rows)
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert "api_key" not in manifest["model_parameters"]


async def test_runs_refuse_reused_or_out_of_tree_outputs_and_keyless_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(study, "REPORTS", tmp_path / "reports")
    (tmp_path / "reports" / "taken").mkdir(parents=True)
    with pytest.raises(ValueError, match="inside workspace reports"):
        await study.execute(_args(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="already exists"):
        await study.execute(_args(tmp_path / "reports" / "taken"))
    monkeypatch.delenv("FORGE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FORGE_API_KEY"):
        await study.execute(_args(tmp_path / "reports" / "live", mode="live"))
    monkeypatch.setenv("FORGE_API_KEY", "not-used")
    with pytest.raises(ValueError, match="sealed protocol"):
        await study.execute(_args(tmp_path / "reports" / "eval", mode="live", split="evaluation"))


async def test_a_protocol_cannot_be_sealed_from_a_contract_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(study, "REPORTS", tmp_path)
    await study.execute(_args(tmp_path / "dev"))
    with pytest.raises(ValueError, match="complete live dev run"):
        study.seal(argparse.Namespace(dev_report=tmp_path / "dev", protocol=tmp_path / "p.json"))

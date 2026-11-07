"""Wiring and grading of the collaboration scaling study, without any model service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
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
    assert all(row["attempt"] == 0 and row["discarded_attempts"] == [] for row in rows)
    assert len(list((tmp_path / "run").glob("*/*/attempt-0/result.json"))) == 3
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


async def test_the_meter_records_status_usage_and_per_call_latency() -> None:
    replies = iter(
        [
            httpx.Response(503, text="busy"),
            httpx.Response(
                200,
                json={
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 7,
                        "prompt_cache_hit_tokens": 64,
                    }
                },
            ),
        ]
    )
    meter = study.Meter()
    transport = httpx.MockTransport(lambda request: next(replies))
    async with httpx.AsyncClient(
        transport=transport,
        event_hooks={"request": [meter.on_request], "response": [meter.on_response]},
    ) as client:
        await client.post("https://provider.invalid/chat")
        await client.post("https://provider.invalid/chat")
    assert meter.requests == 2
    failed, ok = meter.responses
    assert failed["status"] == 503 and failed["complete"] is False
    assert ok == {
        **ok, "status": 200, "complete": True, "prompt": 100, "completion": 7, "cached": 64
    }  # fmt: skip
    assert all(e["seconds"] is not None and e["seconds"] >= e["headers_seconds"] >= 0
               for e in meter.responses)  # fmt: skip


def _row(**fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "error": None,
        "http_statuses": [200],
        "missing_responses": 0,
        "seconds": 1.0,
        "cost_usd_peak": 0.001,
        "infrastructure_retryable": False,
    }
    row.update(fields)
    return row


async def test_only_provider_side_failures_are_rerun_and_every_attempt_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script: list[dict[str, object]] = []
    seen: list[Path] = []

    async def fake_run_one(case, arm, sample, args, destination):  # type: ignore[no-untyped-def]
        seen.append(destination)
        return dict(script.pop(0))

    monkeypatch.setattr(study, "run_one", fake_run_one)
    monkeypatch.setattr(study, "INFRA_BACKOFF_SECONDS", (0.0,))
    case = study.cases("dev")[0]

    script[:] = [
        _row(http_statuses=[503], infrastructure_retryable=True, error="HTTPStatusError"),
        _row(),
    ]
    row = await study.run_measured(case, "react", 0, None, tmp_path / "a")
    assert row["attempt"] == 1 and [d["http_statuses"] for d in row["discarded_attempts"]] == [
        [503]
    ]
    assert seen == [tmp_path / "a" / "attempt-0", tmp_path / "a" / "attempt-1"]
    assert json.loads((tmp_path / "a" / "result.json").read_text())["attempt"] == 1

    # A client error would recur on every attempt: graded as it is, never rerun.
    script[:] = [_row(http_statuses=[400], infrastructure_retryable=False)]
    row = await study.run_measured(case, "react", 0, None, tmp_path / "b")
    assert row["attempt"] == 0 and row["discarded_attempts"] == []

    # Persistent provider trouble stops at the cap; the last attempt is what is graded.
    script[:] = [_row(missing_responses=1, infrastructure_retryable=True)] * 3
    row = await study.run_measured(case, "team", 1, None, tmp_path / "c")
    assert row["attempt"] == study.INFRA_ATTEMPTS - 1
    assert len(row["discarded_attempts"]) == study.INFRA_ATTEMPTS - 1


def test_sealing_requires_a_clean_dev_run_with_parseable_answers(tmp_path: Path) -> None:
    def dev(name: str, **summary: object) -> Path:
        folder = tmp_path / name
        study.write(folder / "manifest.json", {"mode": "live", "identity": study.identity(),
                                               "model": "deepseek-flash"})  # fmt: skip
        study.write(
            folder / "summary.json",
            {"complete": True, "measurement": True, "malformed_or_failed_runs": 0, **summary},
        )
        return folder

    def seal(folder: Path, protocol: str) -> None:
        study.seal(argparse.Namespace(dev_report=folder, protocol=tmp_path / protocol))

    with pytest.raises(ValueError, match="not a clean measurement"):
        seal(dev("infra", measurement=False), "p1.json")
    with pytest.raises(ValueError, match="no parseable answer"):
        seal(dev("malformed", malformed_or_failed_runs=1), "p2.json")
    seal(dev("clean"), "p3.json")
    protocol = json.loads((tmp_path / "p3.json").read_text())
    assert protocol["samples"] == 2 and protocol["decision_rule"] == study.DECISION_RULE
    with pytest.raises(ValueError, match="immutable"):
        seal(dev("again"), "p3.json")

"""The collaboration ablation: fresh tasks, faithful arms and the pre-registered rule."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from test_multi_material_cases import _rule_check

import benchmarks.collaboration_ablation as ablation
import benchmarks.collaboration_scaling as scaling
from evals.multi_material_ablation_cases import ABLATION_CASES, ablation_cases, generate
from evals.multi_material_cases import cases
from forgeharness.models.prepared import canonical_json
from forgeharness.sdk import (
    CredentialMask,
    FinalAction,
    Message,
    MessageRole,
    ModelRequest,
    ModelResult,
    ModelUsage,
    PreparedModelCall,
)


def test_the_generator_is_the_scaling_studys_with_an_explicit_seed() -> None:
    for case in (*cases("dev"), *cases("evaluation")):
        number = int(case.id.rsplit("-", 1)[1])
        seed = 1101 if case.split == "dev" else 2202
        assert generate(seed, case.split, case.size, number) == case


def test_ablation_tasks_are_fresh_labelled_and_mask_safe() -> None:
    tasks = ablation_cases()
    assert tasks == ablation_cases() and len(tasks) == ABLATION_CASES
    assert {task.size for task in tasks} == {32}
    old = {s.text for c in (*cases("dev"), *cases("evaluation")) for s in c.services}
    mask = CredentialMask()
    for task in tasks:
        assert 1 <= len(task.violations) < task.size
        for service in task.services:
            assert service.text not in old
            assert _rule_check(service) == service.violation, (task.id, service.id)
            assert mask.transform(service.text) == service.text


def test_the_team_arm_is_the_scaling_studys_team_arm() -> None:
    for case in (ablation_cases()[0], next(c for c in cases("dev") if c.size == 32)):
        assert ablation.arm_plan(case, "team", "m") == scaling.team_plan(case, "m")
        first = ablation.arm_plan(case, "team-findings-first", "m")
        assert first.agents[0].instructions == scaling.SPECIALIST + (
            " Write the findings field before the summary field; write the summary last."
        )
        assert first.tasks == scaling.team_plan(case, "m").tasks


def test_halves_keep_service_ids_and_give_two_services_per_specialist() -> None:
    case = ablation_cases()[0]
    left, right = ablation.halves(case)
    assert left.services + right.services == case.services
    assert left.violations + right.violations == case.violations
    for half in (left, right):
        plan = ablation.arm_plan(half, "team-load2", "m")
        assert {len(task.evidence_ids) for task in plan.tasks} == {2}


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[ModelRequest] = []

    async def decide(self, request: ModelRequest) -> ModelResult:
        self.seen.append(request)
        return ModelResult(
            action=FinalAction(content='```json\n{"violations": []}\n```'),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )


def _request() -> ModelRequest:
    return ModelRequest(
        task_id="t",
        messages=(
            Message(role=MessageRole.USER, content="goal"),
            Message(role=MessageRole.SYSTEM, content="expert"),
            Message(role=MessageRole.USER, content="materials"),
        ),
        tools=(),
    )


async def test_system_first_reorders_what_is_sent_and_counts_it() -> None:
    inner, fences, counts = _Recorder(), [0], [0, 0]
    model = ablation.SystemFirst(inner, fences, counts)
    result = await model.decide(_request())
    assert [m.content for m in inner.seen[0].messages] == ["expert", "goal", "materials"]
    assert counts == [1, 1] and fences == [1]
    assert isinstance(result.action, FinalAction) and result.action.content == '{"violations": []}'
    already = ablation.system_first(_request())
    await model.decide(already)
    assert counts == [2, 1]


def test_the_framework_refuses_a_prepared_payload_that_reorders_the_request() -> None:
    """Why the probe lives in ``decide``: the recorded request must be what is sent."""
    request = _request()
    with pytest.raises(ValueError, match="does not match its runtime request"):
        PreparedModelCall(
            request_json=canonical_json(request.model_dump(mode="json")),
            payload_json=canonical_json(ablation.system_first(request).model_dump(mode="json")),
        )


def test_findings_first_reads_key_order_of_the_raw_report() -> None:
    assert ablation.findings_first('{"task_id": "g", "findings": [], "summary": "s"}')
    assert not ablation.findings_first('{"task_id": "g", "summary": "s", "findings": []}')
    assert ablation.findings_first('```json\n{"findings": [], "summary": "s"}\n```')
    assert not ablation.findings_first("{}")


def _arm(successes: int, errors: int, manipulation: bool | None = None) -> dict[str, Any]:
    return {
        "runs": 12,
        "successes": successes,
        "service_errors": errors,
        "manipulation_ok": manipulation,
    }


def test_the_pre_registered_attribution_rule() -> None:
    arms = {
        "oneshot": _arm(12, 0),
        "team": _arm(3, 14),
        "team-system-first": _arm(11, 2, True),
        "team-findings-first": _arm(12, 0, False),
        "team-load2": _arm(5, 10),
        "split-oneshot": _arm(8, 9),
    }
    verdict = ablation.attribute(arms)
    assert verdict == {
        "replicated": True,
        "variants": {
            "team-system-first": "removes_failure",
            "team-findings-first": "not_interpreted:manipulation_failed",
            "team-load2": "no_effect",
            # Four more successes, but errors not halved: not enough to attribute.
            "split-oneshot": "inconclusive",
        },
    }
    arms["team"] = _arm(9, 3)
    assert ablation.attribute(arms)["replicated"] is False
    assert set(ablation.attribute(arms)["variants"].values()) == {"not_attributed:no_replication"}
    arms["team"], arms["oneshot"] = _arm(3, 14), _arm(10, 2)
    assert ablation.attribute(arms)["replicated"] is False


def _args(output: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "split": "dev",
        "mode": "contract",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "output": output,
        "protocol": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


async def test_contract_run_wires_every_arm_and_both_manipulations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ablation, "REPORTS", tmp_path)
    summary = await ablation.execute(_args(tmp_path / "dev"))
    arms = summary["arms"]
    assert summary["complete"] is True and summary["measurement"] is False
    assert {arm: data["successes"] for arm, data in arms.items()} == dict.fromkeys(ablation.ARMS, 1)
    # Every collaboration request carries the H2 premise in its runtime order ...
    for arm in ("team", "team-system-first", "team-findings-first", "team-load2"):
        assert arms[arm]["system_after_user_requests"] == arms[arm]["specialist_requests"] > 0
        assert arms[arm]["coordinator_dropped"] == arms[arm]["coordinator_added"] == 0
    # ... and only the probe arm reorders it, every specialist and coordinator request.
    assert arms["team-system-first"]["reordered_requests"] == 9
    assert arms["team-system-first"]["manipulation_ok"] is True
    assert arms["team"]["reordered_requests"] == 0
    assert arms["team-load2"]["specialist_reports"] == 16
    # The oracle writes its summary first, so the findings-first check must catch it.
    assert arms["team-findings-first"]["manipulation_ok"] is False
    rows = [json.loads(p.read_text()) for p in (tmp_path / "dev").glob("*/*/result.json")]
    assert len(rows) == len(ablation.ARMS) and all(row["trace_valid"] for row in rows)
    manifest = json.loads((tmp_path / "dev" / "manifest.json").read_text())
    assert "api_key" not in manifest["model_parameters"]

    with pytest.raises(ValueError, match="complete live dev run"):
        ablation.seal(argparse.Namespace(dev_report=tmp_path / "dev", protocol=tmp_path / "p.json"))


async def test_the_ablation_split_needs_a_sealed_protocol_in_live_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ablation, "REPORTS", tmp_path)
    monkeypatch.setenv("FORGE_API_KEY", "not-used")
    with pytest.raises(ValueError, match="sealed protocol"):
        await ablation.execute(_args(tmp_path / "run", mode="live", split="ablation"))
    with pytest.raises(ValueError, match="inside workspace reports"):
        await ablation.execute(_args(tmp_path.parent / "elsewhere"))


def test_sealing_needs_clean_parseable_dev_with_working_manipulations(tmp_path: Path) -> None:
    def dev(name: str, arms: dict[str, Any] | None = None, **summary: object) -> Path:
        folder = tmp_path / name
        scaling.write(
            folder / "manifest.json",
            {"mode": "live", "split": "dev", "identity": ablation.identity(), "model": "m"},
        )
        scaling.write(
            folder / "summary.json",
            {
                "complete": True,
                "measurement": True,
                "malformed_or_failed_runs": 0,
                "arms": arms or {"team-findings-first": {"manipulation_ok": True}},
                **summary,
            },
        )
        return folder

    def seal(folder: Path, protocol: str) -> None:
        ablation.seal(argparse.Namespace(dev_report=folder, protocol=tmp_path / protocol))

    with pytest.raises(ValueError, match="not a clean measurement"):
        seal(dev("infra", measurement=False), "p1.json")
    with pytest.raises(ValueError, match="no parseable answer"):
        seal(dev("malformed", malformed_or_failed_runs=1), "p2.json")
    with pytest.raises(ValueError, match="manipulation check failed"):
        seal(dev("ignored", {"team-findings-first": {"manipulation_ok": False}}), "p3.json")
    seal(dev("clean"), "p4.json")
    protocol = json.loads((tmp_path / "p4.json").read_text())
    assert protocol["rule"] == ablation.ABLATION_RULE and protocol["planned_runs"] == 72
    assert protocol["ablation_cases"] == [c.id for c in ablation_cases()]

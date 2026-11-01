import json
from argparse import Namespace

import httpx
import pytest

from benchmarks import strategy_comparison as comparison
from benchmarks.strategy_comparison import ARMS, ExactAnswer, cluster_interval, run_one, summarize
from evals.strategy_cases import DEV, EVALUATION
from forgeharness.sdk import FinalAction, VerificationRequest


async def test_three_real_harness_paths_charge_all_roles(tmp_path):
    args = Namespace(mode="contract", model="test")
    rows = [await run_one(DEV[0], arm, 0, args, tmp_path / arm) for arm in ARMS]
    assert all(r["success"] and r["trace_valid"] for r in rows)
    assert [r["usage"]["steps"] for r in rows] == [2, 1, 3]
    assert [r["usage"]["tool_calls"] for r in rows] == [1, 3, 3]
    report = summarize(rows, 3, "contract")
    assert report["complete"] and not report["strategy_measurement"]
    assert not report["cost_metrics_valid"]
    assert all(
        m["input_tokens"] is None and m["tokens_per_success_including_failures"] is None
        for m in report["arms"].values()
    )
    rows[0]["success"] = False
    rows[0]["usage_known"] = False
    report = summarize(rows, 3, "local")
    assert report["arms"]["single"]["runs"] == 1
    assert report["arms"]["single"]["success_rate"] == 0
    assert report["arms"]["single"]["input_tokens"] is None
    assert report["arms"]["single"]["tokens_per_success_including_failures"] is None
    rows[0]["infrastructure_error"] = True
    invalid = summarize(rows, 3, "local")
    assert not invalid["strategy_measurement"]
    assert invalid["infrastructure_failures"] == 1
    assert invalid["arms"]["single"]["runs"] == 1
    assert not summarize([*rows, rows[0]], 4, "local")["complete"]


async def test_exact_grading_does_not_accept_completion_claims():
    verifier = ExactAnswer("deny")
    for text, expected in [
        ("I am done", False),
        ('{"answer":"allow"}', False),
        ('{"answer":"deny"}', True),
    ]:
        request = VerificationRequest(
            task_id="test", workspace=None, final=FinalAction(content=text)
        )
        assert (await verifier.verify(request)).passed is expected


def test_repeated_samples_are_clustered_by_task():
    rows = [{"case": "a", "arm": "multi", "success": True}] * 9 + [
        {"case": "b", "arm": "multi", "success": False}
    ]
    # The nine copies do not become nine independent business tasks.
    assert cluster_interval(rows, "multi") == [0, 1]


def test_task_counts_and_template_identity():
    assert len(DEV) == 12 and len(EVALUATION) == 30
    assert len({c.id for c in (*DEV, *EVALUATION)}) == 42
    assert {c.question for c in DEV}.isdisjoint(c.question for c in EVALUATION)
    assert all(
        sum(c.family == family for c in EVALUATION) == 10
        for family in ("parallel", "chain", "error")
    )


@pytest.mark.parametrize("value", ["{}", "[]", '"deny"', '{"answer":"deny","extra":1}'])
async def test_grade_schema_is_not_relaxed(value):
    result = await ExactAnswer("deny").verify(
        VerificationRequest(task_id="test", workspace=None, final=FinalAction(content=value))
    )
    assert not result.passed


async def test_http_507_is_retained_but_invalidates_measurement(tmp_path, monkeypatch):
    original = httpx.AsyncClient

    def client(**kwargs):
        return original(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(507, json={"error": "synthetic memory limit"})
            ),
        )

    monkeypatch.setattr(comparison.httpx, "AsyncClient", client)
    args = Namespace(mode="local", model="test")
    rows = [await run_one(DEV[0], arm, 0, args, tmp_path / arm) for arm in ARMS]
    assert all(not r["success"] and r["infrastructure_error"] for r in rows)
    report = summarize(rows, 3, "local")
    assert report["actual_runs"] == 3 and report["complete"]
    assert not report["strategy_measurement"] and not report["cost_metrics_valid"]
    assert report["infrastructure_failures"] == 3


async def test_memory_preflight_does_not_start_or_seal_sampling(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, "ROOT", tmp_path)

    async def unavailable(model):
        return {"ready": False, "dynamic_ceiling_bytes": 1}

    monkeypatch.setattr(comparison, "readiness", unavailable)
    output = tmp_path / "reports/dev"
    with pytest.raises(SystemExit) as exc:
        await comparison.execute(
            Namespace(
                output=output, split="dev", mode="local", model="test", samples=1, protocol=None
            )
        )
    assert exc.value.code == 2
    assert (output / "preflight.json").exists()
    assert not (output / "manifest.json").exists()


@pytest.mark.parametrize("arm,expected_calls", [("fixed", 1), ("multi", 3)])
async def test_final_only_phases_do_not_advertise_unexecutable_tools(
    tmp_path, monkeypatch, arm, expected_calls
):
    original = httpx.AsyncClient
    payloads = []

    def respond(request):
        body = json.loads(request.content)
        payloads.append(body)
        assert "tools" not in body and "tool_choice" not in body
        content = (
            '{"keys":["0","1","2"]}'
            if "PLANNER_KEYS" in body["messages"][-1]["content"]
            else '{"answer":"auth,trace"}'
        )
        return httpx.Response(
            200,
            json={
                "model": "fixture",
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(comparison.httpx, "AsyncClient", client)
    row = await run_one(DEV[0], arm, 0, Namespace(mode="local", model="fixture"), tmp_path)
    assert row["success"] and row["usage_known"] and row["trace_valid"]
    assert len(payloads) == expected_calls
    assert row["usage"]["steps"] == expected_calls and row["usage"]["tool_calls"] == 3

"""Benchmark evidence must not turn synthetic, incomplete or fast failures into wins."""

from argparse import Namespace
from dataclasses import asdict

import pytest

from benchmarks import parallel_execution as benchmark


def rows(*, fail=False, missing=False):
    values = []
    for family in ("fork", "chain", "uneven"):
        for i in range(3):
            for limit in (1, 2):
                values.append(
                    {
                        "case": f"{family}-{i}",
                        "family": family,
                        "limit": limit,
                        "sample": 0,
                        "success": not (fail and limit == 2),
                        "seconds": 1 if limit == 2 else 2,
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "usage_known": not missing,
                        "trace_valid": True,
                        "infrastructure_error": False,
                        "peak_client_requests": limit,
                    }
                )
    return values


def test_synthetic_timing_and_tokens_never_qualify_performance():
    summary = benchmark.summarize(rows(), 18, mode="contract")
    assert summary["complete"] and not summary["measurement_valid"]
    assert not summary["performance_qualified"]
    assert all(f["recommended_max_in_flight"] == 1 for f in summary["families"].values())


@pytest.mark.parametrize(
    "failure", ["quality", "unknown_usage", "duplicate", "missing_pair", "infrastructure"]
)
def test_missing_or_failed_evidence_cannot_manufacture_speedup(failure):
    data = rows(fail=failure == "quality", missing=failure == "unknown_usage")
    if failure == "duplicate":
        data[-1] = data[-2]
    elif failure == "missing_pair":
        data[-1] = {**data[-1], "case": "unpaired"}
    elif failure == "infrastructure":
        data[-1] = {**data[-1], "infrastructure_error": True}
    summary = benchmark.summarize(data, 18, mode="local")
    assert not summary["performance_qualified"]


def test_complete_local_pairs_use_quality_and_cost_guards():
    summary = benchmark.summarize(rows(), 18, mode="local")
    assert summary["performance_qualified"]
    assert summary["families"]["fork"]["paired_quality_95"] == [0, 0]
    assert summary["families"]["chain"]["recommended_max_in_flight"] == 1
    assert summary["business_qualified"] is False


def test_corpus_is_distinct_and_graph_only_changes_real_dependencies():
    dev, evaluation = [benchmark.cases(split) for split in ("dev", "evaluation")]
    assert len(dev) == 12 and len(evaluation) == 30
    assert not {c.id for c in dev} & {c.id for c in evaluation}
    assert not {str(asdict(c)) for c in dev} & {str(asdict(c)) for c in evaluation}
    for case in dev:
        graph = benchmark.definition(case)
        assert len(graph.nodes) == 3
        assert graph.nodes[1].dependencies == (("s0",) if case.family == "chain" else ())
        assert graph.nodes[2].dependencies == ("s0", "s1")


async def test_keyless_runner_preserves_outputs_and_measures_both_limits(tmp_path, monkeypatch):
    # A workspace copy is the same path discipline used by the clean release gate.
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    args = Namespace(
        mode="contract",
        split="dev",
        samples=1,
        model="synthetic",
        output=tmp_path / "reports/contract",
        protocol=None,
    )
    await benchmark.execute(args)
    import json

    report = json.loads((args.output / "summary.json").read_text())
    assert report["complete"] and report["actual_runs"] == 24
    assert report["families"]["fork"]["arms"]["2"]["peak_client_requests"] == 2
    assert report["families"]["chain"]["arms"]["2"]["peak_client_requests"] == 1
    assert not report["measurement_valid"]
    with pytest.raises(ValueError, match="previous attempts"):
        await benchmark.execute(args)
    with pytest.raises(ValueError, match="live development"):
        benchmark.seal(
            Namespace(protocol=tmp_path / "reports/protocol.json", dev_report=args.output)
        )


async def test_formal_sampling_requires_sealed_protocol_and_report_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    args = Namespace(
        mode="local",
        split="evaluation",
        samples=3,
        model="synthetic",
        output=tmp_path / "reports/evaluation",
        protocol=None,
    )
    with pytest.raises(ValueError, match="frozen"):
        await benchmark.execute(args)
    args.output = tmp_path / "outside"
    with pytest.raises(ValueError, match="workspace reports"):
        await benchmark.execute(args)

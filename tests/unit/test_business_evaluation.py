import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from benchmarks.business_evaluation import ROOT, evaluate, load_dev
from benchmarks.business_seal import Targets, freeze_targets, open_sealed


def test_development_and_sealed_metadata_have_disjoint_families():
    cases = load_dev(ROOT / "evals/business/development.json")
    metadata = json.loads((ROOT / "evals/business/sealed-metadata.json").read_text())
    assert metadata["count"] == 60 and not metadata["opened_for_evaluation"]
    assert set(metadata["families"]).isdisjoint(c["family"] for c in cases)
    controls = json.loads((ROOT / "evals/business/safety.json").read_text())["nodeids"]
    assert len(set(controls)) == len(controls) >= 40


def declaration(raw, **updates):
    values = dict(
        sealed_sha256=hashlib.sha256(raw).hexdigest(),
        scoring_version="1",
        frozen_at=datetime.now(UTC) - timedelta(seconds=1),
        thresholds={"exact": 1.0},
        abort_conditions=("any scope escape",),
        model="test fixture",
        source_identity="a" * 64,
        reviewer="test",
    )
    values.update(updates)
    return Targets(**values)


def test_seal_requires_prior_freeze_and_cannot_be_reopened_even_after_bad_json(tmp_path):
    data = tmp_path / "sealed.json"
    data.write_text("not json")
    target = tmp_path / "targets.json"
    marks = tmp_path / "markers"
    with pytest.raises(FileNotFoundError):
        open_sealed(data, targets_path=target, marker_dir=marks)
    freeze_targets(target, declaration(data.read_bytes()))
    with pytest.raises(FileExistsError):
        freeze_targets(target, declaration(data.read_bytes()))
    with pytest.raises(ValueError):
        open_sealed(data, targets_path=target, marker_dir=marks)
    assert len(list(marks.iterdir())) == 1
    with pytest.raises(FileExistsError):
        open_sealed(data, targets_path=target, marker_dir=marks)


def test_seal_refuses_changed_data_and_future_targets(tmp_path):
    data = tmp_path / "sealed.json"
    data.write_text("{}")
    target = tmp_path / "targets.json"
    with pytest.raises(ValueError, match="timestamp"):
        freeze_targets(target, declaration(b"{}", frozen_at=datetime.now(UTC) + timedelta(days=1)))
    freeze_targets(target, declaration(b"{}"))
    data.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="differs"):
        open_sealed(data, targets_path=target, marker_dir=tmp_path / "markers")
    data.write_text("{}")
    assert open_sealed(data, targets_path=target, marker_dir=tmp_path / "markers") == {}


async def test_keyless_report_cannot_be_a_business_quality_claim(tmp_path, monkeypatch):
    from benchmarks import business_evaluation as evaluation

    async def result(case, root):
        return {"passed": True}

    monkeypatch.setattr(evaluation, "legal_case", result)
    monkeypatch.setattr(evaluation, "commerce_case", result)
    report = await evaluate(ROOT / "evals/business/development.json", tmp_path / "report.json")
    assert report["passed"] and report["case_count"] == 120
    assert report["measurement_valid"] is False
    assert report["business_qualified"] is False
    assert report["independent_business_questions"] is None

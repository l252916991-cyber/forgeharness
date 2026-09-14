"""Tests for the keyless CLI and review-prerequisite command."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forgeharness.cli import app
from forgeharness.review import REQUIRED_DOCUMENTS, REQUIRED_EVIDENCE, main, snapshot_tree_hash

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip terminal styling; CI forces color and splits tokens like ``--model``."""
    return _ANSI.sub("", text)


def test_demo_command_runs_keyless_vertical_slice() -> None:
    result = CliRunner().invoke(app, ["demo", "--text", "tested"])

    assert result.exit_code == 0
    assert "status=succeeded" in result.stdout
    assert "output=Echo verified: tested" in result.stdout
    assert "trace_events=8" in result.stdout


def test_control_eval_command_writes_report(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    output = tmp_path / "control.json"
    result = CliRunner().invoke(
        app,
        [
            "eval-control",
            "--manifest",
            str(project_root / "evals" / "control_cases.json"),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "control_eval=7/7" in result.stdout
    assert output.is_file()


def test_repair_command_requires_model_and_key() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["repair", ".", "fix it"])

    assert result.exit_code != 0
    assert "--model" in _plain(result.stderr)

    missing_key = runner.invoke(app, ["repair", ".", "fix it", "--model", "test"])
    assert missing_key.exit_code != 0
    assert "--api-key" in _plain(missing_key.stderr)


def test_review_reports_missing_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit, match="missing required documents"):
        main()


@pytest.fixture
def audited_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for relative in (*REQUIRED_DOCUMENTS, *REQUIRED_EVIDENCE):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    manifest = {"schema_version": 1, "cases": []}
    manifest_bytes = json.dumps(manifest).encode()
    (tmp_path / "evals/control_cases.json").write_bytes(manifest_bytes)
    revision = "a" * 40
    empty_snapshot = snapshot_tree_hash(())
    (tmp_path / "reports/control-eval.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-09-01T00:00:00Z",
                "revision": revision,
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "total": 0,
                "passed": 0,
                "cases": [],
            }
        )
    )
    (tmp_path / "docs/review/FINDINGS.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_revision": revision,
                "candidate_snapshot_sha256": empty_snapshot,
                "audit_status": "passed",
                "findings": [],
            }
        )
    )
    (tmp_path / "reports/candidate-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-09-03T00:00:00Z",
                "revision": revision,
                "source_tree_sha256": empty_snapshot,
                "files": [],
            }
        )
    )
    for name, body in {
        "rag-eval.json": {
            "revision": revision,
            "qualified": True,
            "total": 60,
            "recall_at_5": 1,
            "citation_precision": 1,
            "unsupported_answer_rate": 0,
            "mrr_at_10": 1,
            "fused_mrr_at_10": 1,
        },
        "omlx-qualification.json": {
            "revision": revision,
            "qualified": True,
            "quick": False,
            "sections": {
                name: {"total": count, "pass_rate": 1}
                for name, count in [
                    ("tool_calling", 20),
                    ("intent", 20),
                    ("vision", 20),
                    ("embedding", 10),
                    ("reranker", 30),
                ]
            },
        },
        "retrieval-benchmark.json": {
            "revision": revision,
            "qualified": True,
            "chunks": 5_000,
            "concurrency": 10,
            "retrieval_p95_ms": 1,
        },
        "reviewer-experiment.json": {
            "revision": revision,
            "baseline": {"total": 60},
            "reviewer": {"total": 60},
            "quality_improvement_points": 0.0,
            "latency_ratio": 1.0,
            "reviewer_enabled_by_default": False,
        },
        "platform-verification.json": {"revision": revision, "qualified": True},
        "dependency-audit-runtime.json": {
            "dependencies": [{"name": "example", "version": "1", "vulns": []}]
        },
        "coverage.json": {"totals": {"num_branches": 100, "covered_branches": 85}},
    }.items():
        (tmp_path / "reports" / name).write_text(json.dumps(body))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_review_accepts_required_documents(
    audited_candidate: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main()

    assert "review passed" in capsys.readouterr().out
    result = CliRunner().invoke(app, ["review"])
    assert result.exit_code == 0 and "review passed" in result.stdout


@pytest.mark.parametrize(
    ("path", "updates", "error"),
    [
        ("reports/control-eval.json", {"schema_version": 2}, "schema version"),
        ("reports/control-eval.json", {"manifest_sha256": "0" * 64}, "manifest hash"),
        ("reports/control-eval.json", {"total": 1}, "not all control"),
        ("reports/control-eval.json", {"revision": "b" * 40}, "revisions differ"),
        ("reports/candidate-manifest.json", {"revision": "b" * 40}, "review revisions differ"),
        (
            "docs/review/FINDINGS.json",
            {"candidate_snapshot_sha256": "0" * 64},
            "snapshot hashes differ",
        ),
        ("docs/review/FINDINGS.json", {"audit_status": "failed"}, "audit status"),
        (
            "docs/review/FINDINGS.json",
            {
                "findings": [
                    {
                        "id": "FH-999",
                        "severity": "high",
                        "status": "open",
                        "title": "unfinished",
                        "evidence": "test",
                        "resolution": "pending",
                    }
                ]
            },
            "unresolved blocker/high",
        ),
        ("reports/rag-eval.json", {"revision": "b" * 40}, "not bound"),
        ("reports/rag-eval.json", {"qualified": False}, "RAG quality"),
        ("reports/rag-eval.json", {"citation_precision": 0.1}, "RAG quality"),
        ("reports/rag-eval.json", {"recall_at_5": 0.1}, "RAG quality"),
        ("reports/rag-eval.json", {"unsupported_answer_rate": 0.5}, "RAG quality"),
        ("reports/rag-eval.json", {"mrr_at_10": 0.1}, "RAG quality"),
        ("reports/omlx-qualification.json", {"quick": True}, "full OMLX"),
        ("reports/omlx-qualification.json", {"sections": {}}, "OMLX tool_calling"),
        ("reports/retrieval-benchmark.json", {"chunks": 50}, "performance"),
        ("reports/retrieval-benchmark.json", {"retrieval_p95_ms": 1000}, "performance"),
        ("reports/platform-verification.json", {"qualified": False}, "platform"),
        ("reports/reviewer-experiment.json", {"baseline": []}, "malformed"),
        ("reports/reviewer-experiment.json", {"baseline": {"total": 1}}, "at least 60"),
        (
            "reports/reviewer-experiment.json",
            {"reviewer_enabled_by_default": True},
            "without meeting",
        ),
        ("reports/dependency-audit-runtime.json", {"dependencies": []}, "dependency audit"),
        (
            "reports/dependency-audit-runtime.json",
            {"dependencies": [{"vulns": ["CVE-example"]}]},
            "dependency audit",
        ),
        (
            "reports/coverage.json",
            {"totals": {"num_branches": 100, "covered_branches": 84, "percent_covered": 99}},
            "pure branch",
        ),
    ],
)
def test_review_rejects_false_completion(
    audited_candidate: Path, path: str, updates: dict[str, object], error: str
) -> None:
    target = audited_candidate / path
    value = json.loads(target.read_text())
    value.update(updates)
    target.write_text(json.dumps(value))
    with pytest.raises(SystemExit, match=error):
        main()


def test_snapshot_detects_inventory_content_and_hash_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forgeharness import review

    source = tmp_path / "source.py"
    source.write_text("value = 1\n")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports/output.json").write_text("{}")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert review.candidate_paths(tmp_path) == ("source.py",)
    files = (review.snapshot_file(tmp_path, "source.py"),)
    manifest = review.CandidateManifest(
        schema_version=1,
        generated_at="today",
        revision="a" * 40,
        source_tree_sha256=review.snapshot_tree_hash(files),
        files=files,
    )
    errors: list[str] = []
    review._validate_snapshot(tmp_path, manifest, errors)
    assert not errors
    source.write_text("value = 2\n")
    review._validate_snapshot(tmp_path, manifest, errors)
    assert "candidate manifest file hashes are stale" in errors
    assert "candidate manifest source tree hash is stale" in errors
    (tmp_path / "extra.py").write_text("new = True\n")
    errors.clear()
    review._validate_snapshot(tmp_path, manifest, errors)
    assert errors == ["candidate manifest file inventory is stale"]
    errors.clear()
    duplicate = manifest.model_copy(update={"files": files + files})
    review._validate_snapshot(tmp_path, duplicate, errors)
    assert "candidate manifest paths are not unique and sorted" in errors
    (tmp_path / "reports/output.json").write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        review._load_json(tmp_path, "reports/output.json")

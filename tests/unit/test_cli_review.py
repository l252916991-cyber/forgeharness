"""Tests for the keyless CLI and review-prerequisite command."""

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forgeharness.cli import app
from forgeharness.review import REQUIRED_DOCUMENTS, REQUIRED_EVIDENCE, main


def test_demo_command_runs_keyless_vertical_slice() -> None:
    result = CliRunner().invoke(app, ["demo", "--text", "tested"])

    assert result.exit_code == 0
    assert "status=succeeded" in result.stdout
    assert "output=Echo verified: tested" in result.stdout
    assert "trace_events=6" in result.stdout


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
    assert "--model" in result.stderr

    missing_key = runner.invoke(app, ["repair", ".", "fix it", "--model", "test"])
    assert missing_key.exit_code != 0
    assert "--api-key" in missing_key.stderr


def test_review_reports_missing_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit, match="missing required documents"):
        main()


def test_review_accepts_required_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for relative in (*REQUIRED_DOCUMENTS, *REQUIRED_EVIDENCE):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    manifest = {"schema_version": 1, "cases": []}
    manifest_bytes = json.dumps(manifest).encode()
    (tmp_path / "evals/control_cases.json").write_bytes(manifest_bytes)
    revision = "a" * 40
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
                "audit_status": "passed",
                "findings": [],
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    main()

    assert "review passed" in capsys.readouterr().out

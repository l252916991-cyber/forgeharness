"""Tests for manifest-driven deterministic evaluation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from forgeharness.evaluation.control import ControlManifest, run_control_evaluation


async def test_control_evaluation_writes_passing_report(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    output = tmp_path / "reports" / "control.json"

    report = await run_control_evaluation(
        manifest_path=project_root / "evals" / "control_cases.json",
        output_path=output,
        project_root=project_root,
    )

    assert report.total == 7
    assert report.passed == report.total
    assert {case.actual for case in report.cases} >= {"succeeded", "detected", "gated"}
    assert output.is_file()
    assert (
        ControlManifest.model_validate_json(
            (project_root / "evals" / "control_cases.json").read_bytes()
        ).schema_version
        == 1
    )

    repeated = await run_control_evaluation(
        manifest_path=project_root / "evals" / "control_cases.json",
        output_path=output,
        project_root=project_root,
    )
    assert repeated.passed == repeated.total


def test_control_manifest_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        ControlManifest.model_validate(
            {
                "schema_version": 1,
                "cases": [
                    {"id": "same", "kind": "tool_success", "expected": "succeeded"},
                    {"id": "same", "kind": "model_failure", "expected": "failed"},
                ],
            }
        )

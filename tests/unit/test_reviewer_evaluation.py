"""Tests for the Reviewer Agent A/B enablement rule."""

from pathlib import Path

from forgeharness.evaluation.reviewer import run_reviewer_experiment


async def test_reviewer_experiment_keeps_unearned_reviewer_disabled(tmp_path: Path) -> None:
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        """{
          "version": "test-v1",
          "documents": [{
            "source": "agent.md",
            "content": "approval schema protects dangerous agent tools",
            "queries": ["approval schema"]
          }],
          "unanswerable_queries": ["quantum weather satellite"]
        }""",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    report = await run_reviewer_experiment(
        manifest_path=manifest, output_path=output, project_root=tmp_path
    )
    assert report.baseline.effective_answer_rate == 1
    assert report.reviewer.effective_answer_rate == 1
    assert report.quality_improvement_points == 0
    assert report.reviewer_enabled_by_default is False
    assert output.is_file()

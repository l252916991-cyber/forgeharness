"""Tests for the fixed RAG quality gate and report provenance."""

from pathlib import Path

from forgeharness.evaluation.rag import run_rag_evaluation


async def test_fixed_rag_evaluation_meets_declared_gates(tmp_path: Path) -> None:
    project = Path(__file__).parents[2]
    output = tmp_path / "rag-report.json"
    report = await run_rag_evaluation(
        manifest_path=project / "evals" / "rag_cases.json",
        output_path=output,
        project_root=project,
    )
    assert report.total == 60
    assert report.qualified is True
    assert report.mrr_at_10 >= report.fused_mrr_at_10
    assert report.unsupported_answer_rate <= 0.10
    assert '"manifest_version": "rag-v1-60-cases"' in output.read_text()

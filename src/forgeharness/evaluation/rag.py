"""Reproducible keyless RAG evaluation with source-grounded metrics."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import FrozenModel
from forgeharness.knowledge.application import KnowledgeApplication, build_knowledge_application


class _DocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    content: str
    queries: tuple[str, ...] = Field(min_length=1)


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    documents: tuple[_DocumentSpec, ...]
    unanswerable_queries: tuple[str, ...]


class RAGCaseOutcome(FrozenModel):
    id: str
    query: str
    answerable: bool
    relevant_source: str | None
    final_sources: tuple[str, ...]
    recall_at_5: float = Field(ge=0, le=1)
    reciprocal_rank: float = Field(ge=0, le=1)
    fused_reciprocal_rank: float = Field(ge=0, le=1)
    citation_precision: float = Field(ge=0, le=1)
    unsupported: bool


class RAGEvaluationReport(FrozenModel):
    created_at: datetime
    revision: str
    manifest_version: str
    total: int
    recall_at_5: float
    mrr_at_10: float
    fused_mrr_at_10: float
    citation_precision: float
    unsupported_answer_rate: float
    thresholds: dict[str, float]
    qualified: bool
    cases: tuple[RAGCaseOutcome, ...]


async def run_rag_evaluation(
    *, manifest_path: Path, output_path: Path, project_root: Path
) -> RAGEvaluationReport:
    """Build a fresh keyless index, run every fixed case, and persist evidence."""
    manifest_text = await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
    manifest = _Manifest.model_validate_json(manifest_text)
    with tempfile.TemporaryDirectory(prefix="forgeharness-rag-") as temporary:
        application = build_knowledge_application(Path(temporary))
        try:
            for document in manifest.documents:
                await application.knowledge.ingest(
                    filename=document.source,
                    data=document.content.encode("utf-8"),
                )
            outcomes = await _evaluate_cases(application, manifest)
        finally:
            await application.close()
    answerable = tuple(item for item in outcomes if item.answerable)
    unanswerable = tuple(item for item in outcomes if not item.answerable)
    recall = _mean(tuple(item.recall_at_5 for item in answerable))
    mrr = _mean(tuple(item.reciprocal_rank for item in answerable))
    fused_mrr = _mean(tuple(item.fused_reciprocal_rank for item in answerable))
    citation_precision = _mean(tuple(item.citation_precision for item in answerable))
    unsupported = _mean(tuple(float(item.unsupported) for item in unanswerable))
    thresholds = {
        "recall_at_5": 0.85,
        "citation_precision": 0.90,
        "unsupported_answer_rate_max": 0.10,
    }
    report = RAGEvaluationReport(
        created_at=datetime.now(UTC),
        revision=_revision(project_root),
        manifest_version=manifest.version,
        total=len(outcomes),
        recall_at_5=recall,
        mrr_at_10=mrr,
        fused_mrr_at_10=fused_mrr,
        citation_precision=citation_precision,
        unsupported_answer_rate=unsupported,
        thresholds=thresholds,
        qualified=(
            len(outcomes) >= 60
            and recall >= thresholds["recall_at_5"]
            and citation_precision >= thresholds["citation_precision"]
            and unsupported <= thresholds["unsupported_answer_rate_max"]
            and mrr >= fused_mrr
        ),
        cases=outcomes,
    )
    await asyncio.to_thread(_write_report, output_path, report)
    return report


async def _evaluate_cases(
    application: KnowledgeApplication, manifest: _Manifest
) -> tuple[RAGCaseOutcome, ...]:
    knowledge = application.knowledge
    outcomes = []
    case_number = 0
    for document in manifest.documents:
        for query in document.queries:
            case_number += 1
            report = await knowledge.search(query, limit=5)
            response = await knowledge.answer(query, limit=5)
            final_sources = tuple(hit.chunk.source for hit in report.final)
            fused_sources = tuple(hit.chunk.source for hit in report.fused[:10])
            outcomes.append(
                RAGCaseOutcome(
                    id=f"answerable-{case_number:03d}",
                    query=query,
                    answerable=True,
                    relevant_source=document.source,
                    final_sources=final_sources,
                    recall_at_5=float(document.source in final_sources[:5]),
                    reciprocal_rank=_reciprocal_rank(final_sources, document.source),
                    fused_reciprocal_rank=_reciprocal_rank(fused_sources, document.source),
                    citation_precision=_citation_precision(response.citations, final_sources),
                    unsupported=False,
                )
            )
    for query in manifest.unanswerable_queries:
        case_number += 1
        report = await knowledge.search(query, limit=5)
        response = await knowledge.answer(query, limit=5)
        final_sources = tuple(hit.chunk.source for hit in report.final)
        refused = "不知道" in response.answer or "do not know" in response.answer.lower()
        outcomes.append(
            RAGCaseOutcome(
                id=f"unanswerable-{case_number:03d}",
                query=query,
                answerable=False,
                relevant_source=None,
                final_sources=final_sources,
                recall_at_5=1,
                reciprocal_rank=1,
                fused_reciprocal_rank=1,
                citation_precision=1,
                unsupported=not refused,
            )
        )
    return tuple(outcomes)


def _citation_precision(citations: tuple[object, ...], final_sources: tuple[str, ...]) -> float:
    if not citations:
        return 0
    valid = sum(getattr(item, "source", None) in final_sources for item in citations)
    return valid / len(citations)


def _reciprocal_rank(sources: tuple[str, ...], relevant: str) -> float:
    try:
        return 1.0 / (sources.index(relevant) + 1)
    except ValueError:
        return 0


def _mean(values: tuple[float, ...]) -> float:
    return sum(values) / len(values) if values else 0


def _revision(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _write_report(path: Path, report: RAGEvaluationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

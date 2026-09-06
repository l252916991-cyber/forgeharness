"""Controlled A/B experiment for the optional read-only Reviewer Agent."""

from __future__ import annotations

import asyncio
import json
import statistics
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.knowledge.application import KnowledgeSettings, build_knowledge_application


class ReviewerArm(FrozenModel):
    """Observed quality and latency for one fixed experiment arm."""

    correct: int
    total: int
    effective_answer_rate: float = Field(ge=0, le=1)
    median_latency_ms: float = Field(ge=0)


class ReviewerExperimentReport(FrozenModel):
    """Decision evidence for whether Reviewer should be enabled by default."""

    created_at: datetime
    revision: str
    manifest_version: str
    baseline: ReviewerArm
    reviewer: ReviewerArm
    quality_improvement_points: float
    latency_ratio: float
    enablement_threshold_points: float = 5.0
    latency_ratio_max: float = 2.0
    reviewer_enabled_by_default: bool


async def run_reviewer_experiment(
    *, manifest_path: Path, output_path: Path, project_root: Path
) -> ReviewerExperimentReport:
    """Compare identical data/models with the reviewer as the only variable."""
    manifest = json.loads(await asyncio.to_thread(manifest_path.read_text, encoding="utf-8"))
    baseline = await _run_arm(manifest, enable_reviewer=False)
    reviewer = await _run_arm(manifest, enable_reviewer=True)
    improvement = (reviewer.effective_answer_rate - baseline.effective_answer_rate) * 100
    latency_ratio = (
        reviewer.median_latency_ms / baseline.median_latency_ms
        if baseline.median_latency_ms
        else float("inf")
    )
    report = ReviewerExperimentReport(
        created_at=datetime.now(UTC),
        revision=_revision(project_root),
        manifest_version=str(manifest.get("version", "unknown")),
        baseline=baseline,
        reviewer=reviewer,
        quality_improvement_points=improvement,
        latency_ratio=latency_ratio,
        reviewer_enabled_by_default=improvement >= 5 and latency_ratio <= 2,
    )
    await asyncio.to_thread(_write_report, output_path, report)
    return report


async def _run_arm(manifest: dict[str, Any], *, enable_reviewer: bool) -> ReviewerArm:
    with tempfile.TemporaryDirectory(prefix="forgeharness-reviewer-") as temporary:
        application = build_knowledge_application(
            Path(temporary), KnowledgeSettings(enable_reviewer=enable_reviewer)
        )
        try:
            cases: list[tuple[str, bool]] = []
            for document in manifest["documents"]:
                await application.knowledge.ingest(
                    filename=document["source"], data=document["content"].encode("utf-8")
                )
                cases.extend((query, True) for query in document["queries"])
            cases.extend((query, False) for query in manifest["unanswerable_queries"])
            outcomes = []
            latencies = []
            for query, answerable in cases:
                started = time.perf_counter()
                response = await application.knowledge.answer(query, limit=5)
                latencies.append((time.perf_counter() - started) * 1_000)
                refused = "不知道" in response.answer or "do not know" in response.answer.lower()
                outcomes.append(bool(response.citations) and not refused if answerable else refused)
        finally:
            await application.close()
    correct = sum(outcomes)
    return ReviewerArm(
        correct=correct,
        total=len(outcomes),
        effective_answer_rate=correct / len(outcomes),
        median_latency_ms=statistics.median(latencies),
    )


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


def _write_report(path: Path, report: ReviewerExperimentReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

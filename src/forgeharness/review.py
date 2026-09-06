"""Machine-checkable repository gate for the final audit candidate."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.evaluation.control import ControlManifest, ControlReport

REQUIRED_DOCUMENTS = (
    "README.md",
    "LICENSE",
    "docs/PRODUCT_SPEC.md",
    "docs/ARCHITECTURE.md",
    "docs/DELIVERY_PLAN.md",
    "docs/EVALUATION.md",
    "docs/STATUS.md",
    "docs/REFERENCES.md",
    "docs/REVIEW.md",
)

REQUIRED_EVIDENCE = (
    "evals/control_cases.json",
    "reports/control-eval.json",
    "reports/rag-eval.json",
    "reports/omlx-qualification.json",
    "reports/retrieval-benchmark.json",
    "reports/reviewer-experiment.json",
    "reports/platform-verification.json",
    "reports/dependency-audit-runtime.json",
    "reports/coverage.json",
    "reports/candidate-manifest.json",
    "docs/review/FINDINGS.json",
    "docs/review/AUDIT.md",
)

SNAPSHOT_EXCLUDED_PREFIXES = ("reports/", "docs/review/")
SNAPSHOT_EXCLUDED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
SNAPSHOT_EXCLUDED_SUFFIXES = (".pyc", ".sqlite", ".sqlite3", ".log")


class ReviewFinding(FrozenModel):
    """One release finding and its final disposition."""

    id: str = Field(pattern=r"^FH-[0-9]{3}$")
    severity: Literal["blocker", "high", "medium", "low"]
    status: Literal["open", "closed", "accepted"]
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    resolution: str = Field(min_length=1)


class ReviewRecord(FrozenModel):
    """Machine-readable release decision tied to an evaluated revision."""

    schema_version: int = Field(ge=1)
    candidate_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    candidate_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    audit_status: Literal["passed", "failed"]
    findings: tuple[ReviewFinding, ...]


class CandidateFile(FrozenModel):
    """One immutable file record in the exact candidate snapshot."""

    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CandidateManifest(FrozenModel):
    """Exact source, test, configuration, and documentation snapshot."""

    schema_version: int = Field(ge=1)
    generated_at: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_tree_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: tuple[CandidateFile, ...]


def candidate_paths(root: Path) -> tuple[str, ...]:
    """Return tracked and unignored source paths included in a release snapshot."""
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = completed.stdout.decode().split("\0")
    return tuple(
        sorted(
            path
            for path in paths
            if path
            and not path.startswith(SNAPSHOT_EXCLUDED_PREFIXES)
            and not any(part in SNAPSHOT_EXCLUDED_PARTS for part in Path(path).parts)
            and not path.endswith(SNAPSHOT_EXCLUDED_SUFFIXES)
            and (root / path).is_file()
        )
    )


def snapshot_file(root: Path, relative: str) -> CandidateFile:
    """Hash one candidate file without interpreting its contents."""
    content = (root / relative).read_bytes()
    return CandidateFile(
        path=relative, size=len(content), sha256=hashlib.sha256(content).hexdigest()
    )


def snapshot_tree_hash(files: tuple[CandidateFile, ...]) -> str:
    """Hash an ordered, unambiguous representation of all file records."""
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.path.encode())
        digest.update(b"\0")
        digest.update(str(item.size).encode())
        digest.update(b"\0")
        digest.update(item.sha256.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _load_json(root: Path, relative: str) -> dict[str, Any]:
    value = json.loads((root / relative).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{relative} must contain a JSON object")
    return value


def _report_revision(
    report: dict[str, Any], relative: str, revision: str, errors: list[str]
) -> None:
    if report.get("revision") != revision:
        errors.append(f"{relative} is not bound to candidate revision")


def _validate_snapshot(root: Path, manifest: CandidateManifest, errors: list[str]) -> None:
    declared_paths = tuple(item.path for item in manifest.files)
    if declared_paths != tuple(sorted(set(declared_paths))):
        errors.append("candidate manifest paths are not unique and sorted")
    try:
        actual_paths = candidate_paths(root)
    except (OSError, subprocess.CalledProcessError):
        actual_paths = declared_paths
    if actual_paths != declared_paths:
        errors.append("candidate manifest file inventory is stale")
        return
    actual_files = tuple(snapshot_file(root, relative) for relative in declared_paths)
    if actual_files != manifest.files:
        errors.append("candidate manifest file hashes are stale")
    if snapshot_tree_hash(actual_files) != manifest.source_tree_sha256:
        errors.append("candidate manifest source tree hash is stale")


def _validate_extended_reports(root: Path, revision: str, errors: list[str]) -> None:
    rag = _load_json(root, "reports/rag-eval.json")
    omlx = _load_json(root, "reports/omlx-qualification.json")
    benchmark = _load_json(root, "reports/retrieval-benchmark.json")
    reviewer = _load_json(root, "reports/reviewer-experiment.json")
    platform = _load_json(root, "reports/platform-verification.json")
    dependency = _load_json(root, "reports/dependency-audit-runtime.json")
    coverage = _load_json(root, "reports/coverage.json").get("totals", {})
    reports = {
        "reports/rag-eval.json": rag,
        "reports/omlx-qualification.json": omlx,
        "reports/retrieval-benchmark.json": benchmark,
        "reports/reviewer-experiment.json": reviewer,
        "reports/platform-verification.json": platform,
    }
    for relative, report in reports.items():
        _report_revision(report, relative, revision, errors)
    if (
        rag.get("qualified") is not True
        or int(rag.get("total", 0)) < 60
        or not 0.85 <= float(rag.get("recall_at_5", -1)) <= 1
        or not 0.9 <= float(rag.get("citation_precision", -1)) <= 1
        or not 0 <= float(rag.get("unsupported_answer_rate", 1)) <= 0.1
        or not float(rag.get("mrr_at_10", -1)) >= float(rag.get("fused_mrr_at_10", 2))
    ):
        errors.append("RAG quality gate did not pass at least 60 cases")
    if omlx.get("qualified") is not True or omlx.get("quick") is not False:
        errors.append("full OMLX qualification gate did not pass")
    for name, count, threshold in (
        ("tool_calling", 20, 0.9),
        ("intent", 20, 0.9),
        ("vision", 20, 0.85),
        ("embedding", 10, 0.9),
        ("reranker", 30, 0.8),
    ):
        section = omlx.get("sections", {}).get(name, {})
        if (
            int(section.get("total", 0)) < count
            or not threshold <= float(section.get("pass_rate", -1)) <= 1
        ):
            errors.append(f"OMLX {name} evidence is undersized or failed")
    if (
        benchmark.get("qualified") is not True
        or int(benchmark.get("chunks", 0)) < 5_000
        or int(benchmark.get("concurrency", 0)) != 10
        or not 0 <= float(benchmark.get("retrieval_p95_ms", 500)) < 500
    ):
        errors.append("retrieval performance gate did not pass the declared workload")
    if platform.get("qualified") is not True:
        errors.append("live platform verification gate did not pass")
    baseline = reviewer.get("baseline", {})
    reviewed = reviewer.get("reviewer", {})
    if not isinstance(baseline, dict) or not isinstance(reviewed, dict):
        errors.append("reviewer experiment sections are malformed")
    elif int(baseline.get("total", 0)) < 60 or int(reviewed.get("total", 0)) < 60:
        errors.append("reviewer experiment did not evaluate at least 60 cases per arm")
    if reviewer.get("reviewer_enabled_by_default") is True and (
        float(reviewer.get("quality_improvement_points", 0.0)) < 5.0
        or float(reviewer.get("latency_ratio", float("inf"))) > 2.0
    ):
        errors.append("reviewer is enabled without meeting its quality and latency gates")
    dependencies = dependency.get("dependencies", [])
    if (
        not isinstance(dependencies, list)
        or not dependencies
        or any(not isinstance(item, dict) or item.get("vulns") for item in dependencies)
    ):
        errors.append("runtime dependency audit contains a vulnerability or malformed entry")
    total_branches = int(coverage.get("num_branches", 0))
    covered_branches = int(coverage.get("covered_branches", 0))
    if total_branches <= 0 or not 0.85 <= covered_branches / total_branches <= 1:
        errors.append("pure branch coverage did not reach 85%")


def main() -> None:
    """Validate required evidence, its revision binding, and finding disposition."""
    root = Path.cwd()
    required = (*REQUIRED_DOCUMENTS, *REQUIRED_EVIDENCE)
    missing = [relative for relative in required if not (root / relative).is_file()]
    if missing:
        raise SystemExit(f"review failed; missing required documents: {', '.join(missing)}")

    manifest_path = root / "evals/control_cases.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = ControlManifest.model_validate_json(manifest_bytes)
    report = ControlReport.model_validate_json((root / "reports/control-eval.json").read_bytes())
    review = ReviewRecord.model_validate_json((root / "docs/review/FINDINGS.json").read_bytes())
    candidate = CandidateManifest.model_validate_json(
        (root / "reports/candidate-manifest.json").read_bytes()
    )

    expected_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    errors: list[str] = []
    if report.schema_version != manifest.schema_version:
        errors.append("control report schema version does not match manifest")
    if report.manifest_sha256 != expected_manifest_hash:
        errors.append("control report manifest hash is stale")
    if report.total != len(manifest.cases) or report.passed != report.total:
        errors.append("not all control evaluation cases passed")
    if report.revision != review.candidate_revision:
        errors.append("evaluation and review candidate revisions differ")
    if candidate.revision != review.candidate_revision:
        errors.append("candidate manifest and review revisions differ")
    if candidate.source_tree_sha256 != review.candidate_snapshot_sha256:
        errors.append("candidate manifest and review snapshot hashes differ")
    _validate_snapshot(root, candidate, errors)
    _validate_extended_reports(root, review.candidate_revision, errors)
    unresolved_critical = [
        finding.id
        for finding in review.findings
        if finding.severity in {"blocker", "high"} and finding.status != "closed"
    ]
    if unresolved_critical:
        errors.append(f"unresolved blocker/high findings: {', '.join(unresolved_critical)}")
    if review.audit_status != "passed":
        errors.append("audit status is not passed")
    if errors:
        raise SystemExit("review failed; " + "; ".join(errors))
    print(
        f"review passed for {review.candidate_revision}: "
        f"{report.passed}/{report.total} controls, {len(review.findings)} findings recorded"
    )


if __name__ == "__main__":
    main()

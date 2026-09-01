"""Machine-checkable repository gate for the final audit candidate."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

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
    "docs/RESUME_CN.md",
    "docs/REFERENCES.md",
    "docs/REVIEW.md",
)

REQUIRED_EVIDENCE = (
    "evals/control_cases.json",
    "reports/control-eval.json",
    "docs/review/FINDINGS.json",
    "docs/review/AUDIT.md",
)


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
    audit_status: Literal["passed", "failed"]
    findings: tuple[ReviewFinding, ...]


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

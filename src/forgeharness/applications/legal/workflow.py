"""Legal source extraction and human-review draft using shared SDK modules."""

from forgeharness.applications.records import cited_draft_workflow
from forgeharness.sdk import RevisionStore, WorkflowSpec


def legal_review_workflow(
    *,
    store: RevisionStore,
    evidence_id: str,
    note_id: str,
    business_request_id: str,
    expected_version: int = 0,
    application_id: str = "legal",
) -> WorkflowSpec:
    return cited_draft_workflow(
        application_id=application_id,
        store=store,
        source_id=evidence_id,
        target_id=note_id,
        business_request_id=business_request_id,
        kind="legal_note",
        expected_version=expected_version,
    )

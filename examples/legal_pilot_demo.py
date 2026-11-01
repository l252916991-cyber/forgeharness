"""Keyless legal pilot demo: publish synthetic case pages, ask, cite, draft a note.

Everything here is synthetic fixture material. No LexVault repository, no real matter,
no model credential and no legal conclusion is involved.
"""

import asyncio
import json
from pathlib import Path

from forgeharness.applications.legal.adapter import SyntheticCaseAdapter
from forgeharness.applications.legal.pilot import case_qa_application, publish_case_pages
from forgeharness.applications.legal.workflow import legal_review_workflow
from forgeharness.sdk import (
    Actor,
    Citation,
    FinalAction,
    MessageRole,
    ModelRequest,
    ModelResult,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.sdk.evidence import published_source_validator
from forgeharness.sdk.sessions import SessionService
from forgeharness.sdk.workflows import WorkflowRunner

FIXTURES = Path(__file__).resolve().parents[1] / "data/synthetic/legal"


def identity(role: str) -> RunContext:
    return RunContext(
        application_id="legal-case-qa",
        application_version="1",
        policy_version="1",
        run_id="authenticated-request",
        session_id="case-alpha-session",
        correlation_id="synthetic-request",
        actor=Actor(id=role, roles=(role,)),
        scope=ResourceScope(
            tenant_id="firm-alpha",
            resources=(
                "case:case-alpha",
                "draft:note-1",
                *(f"case:case-alpha/page:{page}" for page in range(1, 7)),
            ),
        ),
    )


class CitingModel:
    """Cites the first evidence span it is shown, like a careful reader would."""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, request: ModelRequest) -> ModelResult:
        self.calls += 1
        if self.calls == 1:
            return ModelResult(
                action=ToolAction(
                    call=ToolCall(
                        id="search",
                        name="search_case",
                        arguments={"case_id": "case-alpha", "query": "transferred"},
                    )
                )
            )
        observation = "{}"
        for message in reversed(request.messages):
            if message.role is MessageRole.TOOL:
                observation = message.content or "{}"
                break
        evidence = json.loads(observation)["evidence"][0]
        return ModelResult(
            action=FinalAction(
                content=json.dumps(
                    {
                        "answer": f"The synthetic record states: {evidence['quote']}",
                        "citations": [
                            {
                                "resource_id": evidence["resource_id"],
                                "version": evidence["version"],
                                "digest": evidence["digest"],
                                "start": evidence["start"],
                                "end": evidence["end"],
                                "quote": evidence["quote"],
                            }
                        ],
                        "insufficient_evidence": False,
                    },
                    ensure_ascii=False,
                )
            )
        )


async def demo(root: Path) -> dict[str, object]:
    store = RevisionStore(root / "records.sqlite", policy=RoleRecordPolicy())
    adapter = SyntheticCaseAdapter(FIXTURES)
    versions = publish_case_pages(
        adapter,
        store,
        reader=identity("editor"),
        reviewer=identity("reviewer"),
        publisher=identity("publisher"),
        case_id="case-alpha",
    )
    service = SessionService(
        case_qa_application(adapter, store, model_factory=CitingModel),
        state_dir=root / "sessions",
        authorize=lambda *_: True,
        validate_sources=published_source_validator(store),
    )
    service.create(identity("editor"))
    turn = await service.submit(
        identity("editor"), request_id="question-1", text="What payment was transferred?"
    )
    assert turn.status is RunStatus.SUCCEEDED, turn.error
    answer = json.loads(turn.output or "{}")
    citation = Citation.model_validate(answer["citations"][0])
    page = store.published(identity("reader"), citation.resource_id)

    note_owner = identity("editor").model_copy(update={"run_id": "note-run"})
    workflow = WorkflowRunner(
        legal_review_workflow(
            store=store,
            evidence_id=citation.resource_id,
            note_id="draft:note-1",
            business_request_id="note-1",
            application_id="legal-case-qa",
        ),
        state_dir=root / "notes",
    )
    pending = await workflow.run(context=note_owner, task="Draft a source-linked note")
    assert pending.status is RunStatus.AWAITING_APPROVAL
    supervisor = note_owner.model_copy(
        update={"actor": Actor(id="supervisor", roles=("supervisor",))}
    )
    grant = workflow.approve(context=note_owner, approver=supervisor)
    completed = await workflow.resume(context=note_owner, grant=grant, approver=supervisor)
    assert completed.status is RunStatus.SUCCEEDED
    note = store.read(identity("editor"), "draft:note-1")
    assert note.state == "draft" and note.reviewer is None

    return {
        "data": "synthetic fixture only",
        "model": "deterministic fixture",
        "published_pages": len(versions),
        "note_state": note.state,
        "execution_approved_by": supervisor.actor.id,
        "note_reviewer": note.reviewer,
        "answer": answer["answer"],
        "cited_page": page.sources[0].locator,
        "cited_version": citation.version,
        "span_verified": page.content[citation.start : citation.end] == citation.quote,
    }


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory(prefix="forge-legal-pilot-") as directory:
        print(json.dumps(asyncio.run(demo(Path(directory))), ensure_ascii=False, indent=2))

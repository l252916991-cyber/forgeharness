"""Exact, current source validation independent of answer semantics."""

from datetime import UTC, datetime, timedelta

import pytest
from test_records import draft, owner, published

from forgeharness.sdk import (
    AllOfVerifier,
    Citation,
    CitationVerifier,
    CitedAnswer,
    FinalAction,
    PublishedContextProvider,
    PublishedRecordTool,
    RevisionStore,
    RoleRecordPolicy,
    ScopeError,
    ToolContext,
    VerificationRequest,
    VerificationResult,
)
from forgeharness.sdk.evidence import RecordReadInput


def request(record, **changes):
    citation = Citation(
        resource_id=record.resource_id,
        version=record.version,
        digest=record.digest,
        start=0,
        end=4,
        quote=record.content[:4],
    )
    citation = citation.model_copy(update=changes)
    answer = CitedAnswer(answer="供人工核对的摘录。", citations=(citation,))
    return VerificationRequest(
        task_id="r",
        workspace=None,
        run_context=owner("reader"),
        final=FinalAction(content=answer.model_dump_json()),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"quote": "伪造文本"},
        {"version": 2},
        {"digest": "0" * 64},
        {"end": 999999},
        {"end": 1, "start": 2},
        {"resource_id": "case:other"},
    ],
)
async def test_exact_span_and_scope(tmp_path, changes):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    record = published(store)
    verdict = await CitationVerifier(store).verify(request(record, **changes))
    assert verdict.passed is (not bool(changes))


async def test_edit_withdraws_previously_valid_citation(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    record = published(store)
    store.save(owner(), draft("new evidence"), expected_version=1, request_id="edit")
    assert not (await CitationVerifier(store).verify(request(record))).passed


async def test_missing_context_and_unstructured_answer_rejected(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    record = published(store)
    verifier = CitationVerifier(store)
    assert not (
        await verifier.verify(request(record).model_copy(update={"run_context": None}))
    ).passed
    assert not (
        await verifier.verify(
            request(record).model_copy(update={"final": FinalAction(content="done")})
        )
    ).passed


async def test_provider_and_tool_return_current_provenance(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    record = published(store)
    provider = PublishedContextProvider(store, ("case:1",))
    materials = await provider.provide(task="review", context=owner("reader"))
    assert materials[0].resource_id == record.resource_id
    assert materials[0].version == "1" and materials[0].required
    tool = PublishedRecordTool(store)
    output = await tool.execute(
        RecordReadInput(resource_id="case:1"), ToolContext(task_id="r", run_context=owner("reader"))
    )
    # Anchors are resource-version-locator triples, so a governed read can be re-checked
    # against the host's current ledger (ADR 016).
    assert output.receipt.evidence_refs == ("case:1@1#physical-page:2",)
    with pytest.raises(ScopeError):
        await tool.execute(RecordReadInput(resource_id="case:1"), ToolContext(task_id="r"))


async def test_verifier_composition_stops_on_business_failure(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    record = published(store)

    class Pending:
        async def verify(self, request):
            return VerificationResult(passed=False, reason="job is queued, not completed")

    verifier = AllOfVerifier((CitationVerifier(store), Pending()))
    verdict = await verifier.verify(request(record))
    assert not verdict.passed and verdict.evidence
    assert (await AllOfVerifier((CitationVerifier(store),)).verify(request(record))).passed
    with pytest.raises(ValueError):
        AllOfVerifier(())


async def test_expired_published_record_cannot_be_used(tmp_path, monkeypatch):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    record = published(store, draft(expires_at=datetime.now(UTC) + timedelta(days=1)))

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr("forgeharness.sdk.records.datetime", FutureClock)
    assert not (await CitationVerifier(store).verify(request(record))).passed

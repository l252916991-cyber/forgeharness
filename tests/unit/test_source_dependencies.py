"""Governed tool-read sources are revalidated before stored output is served (ADR 016).

The tests drive the real published-record tool against a real revision store, so the
withdrawal path exercised here is the same one an application hits in production.
"""

from pathlib import Path

import pytest
from test_sdk import Echo, app, context

from forgeharness.domain.models import Actor
from forgeharness.sdk import (
    ApplicationSpec,
    FinalAction,
    ModelResult,
    PolicyDecision,
    PolicyDecisionType,
    RecordDraft,
    ResourceScope,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
    ScriptedModel,
    SourceAnchor,
    ToolAction,
    ToolCall,
)
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.evidence import PublishedRecordTool, published_source_validator
from forgeharness.sdk.records import RevisionStore
from forgeharness.sdk.sessions import SessionService, SessionUnavailable
from forgeharness.sdk.sources import SourceRef


def always(actor: object, operation: object, session: object) -> bool:
    del actor, operation, session
    return True


def owner(role: str = "editor") -> RunContext:
    return context(
        actor=Actor(id=role, roles=(role,)),
        scope=ResourceScope(tenant_id="firm-a", resources=("case:1", "product:a")),
    )


def published_record(store: RevisionStore) -> None:
    store.save(
        owner(),
        RecordDraft(
            resource_id="case:1",
            kind="case_evidence",
            content="Synthetic case fact: the payment was recorded on 2026-01-02.",
            sources=(SourceAnchor(source="synthetic-case", version="1", locator="page:1"),),
        ),
        expected_version=0,
        request_id="seed",
    )
    store.review(owner("reviewer"), "case:1", expected_version=1, approve=True)
    store.publish(owner("publisher"), "case:1", expected_version=1)


class AllowReads:
    """Permissive policy for this fixture; the real checks live in the session layer."""

    def evaluate(self, request: object) -> PolicyDecision:
        del request
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="fixture allows reads")

    def can_approve(self, *, requester: object, owner: object, call: object) -> bool:
        del requester, owner, call
        return False


def reading_application(store: RevisionStore) -> ApplicationSpec:
    """One governed read tool; the scripted model reads the record and then answers."""
    template = app(Echo(), [])
    return ApplicationSpec(
        id=template.id,
        version=template.version,
        policy_version=template.policy_version,
        model_factory=lambda: ScriptedModel(
            [
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(
                            id="read",
                            name="read_published_record",
                            arguments={"resource_id": "case:1"},
                        )
                    )
                ),
                ModelResult(action=FinalAction(content="Recorded on 2026-01-02.")),
            ]
        ),
        tools=(PublishedRecordTool(store),),
        policy=AllowReads(),
        verifier=template.verifier,
    )


def test_governed_tool_requires_a_source_validator(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    published_record(store)

    with pytest.raises(ValueError, match="source validation"):
        SessionService(reading_application(store), state_dir=tmp_path, authorize=always)


def test_a_governed_tool_declares_itself(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    assert PublishedRecordTool(store).spec.governed_sources is True


def test_ungoverned_tools_keep_the_previous_contract(tmp_path: Path) -> None:
    # The plain echo application registers no governed tool, so no validator is needed
    # and no source dependency is invented.
    service = SessionService(
        app(Echo(), [ModelResult(action=FinalAction(content="ok"))]),
        state_dir=tmp_path,
        authorize=always,
    )
    service.create(owner())


async def test_tool_read_sources_are_recorded_on_the_turn(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    published_record(store)
    service = SessionService(
        reading_application(store),
        state_dir=tmp_path,
        authorize=always,
        validate_sources=lambda identity, refs: True,
    )
    service.create(owner("editor"))
    turn = await service.submit(
        owner("editor"), request_id="one", text="When was the payment recorded?"
    )

    assert turn.status is RunStatus.SUCCEEDED
    assert SourceRef(resource_id="case:1", version="1", locator="page:1") in (
        service.get_turn(owner("editor"), turn.id).sources
    )


async def test_withdrawn_tool_source_blocks_history_inspect_and_next_turn(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    published_record(store)
    identity = owner("editor")

    service = SessionService(
        reading_application(store),
        state_dir=tmp_path,
        authorize=always,
        # The shipped host validator: every reference must still be the published
        # version this caller may read, and an unavailable record fails closed.
        validate_sources=published_source_validator(store),
    )
    service.create(identity)
    turn = await service.submit(identity, request_id="one", text="When was the payment recorded?")
    assert turn.status is RunStatus.SUCCEEDED
    assert service.get_turn(identity, turn.id).sources

    store.withdraw(owner("publisher"), "case:1", expected_version=1)

    with pytest.raises((SessionUnavailable, ScopeError)):
        service.history(identity)
    with pytest.raises((SessionUnavailable, ScopeError)):
        service.get_turn(identity, turn.id)
    with pytest.raises((SessionUnavailable, ScopeError)):
        service.inspect(identity, turn.id)
    # A new turn must not receive the withdrawn answer as context either.
    with pytest.raises((SessionUnavailable, ScopeError)):
        await service.submit(identity, request_id="two", text="And the amount?")


async def test_source_validator_denial_is_not_silently_filtered(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    published_record(store)
    identity = owner("editor")
    service = SessionService(
        reading_application(store),
        state_dir=tmp_path,
        authorize=always,
        validate_sources=lambda current, refs: False,
    )
    service.create(identity)
    with pytest.raises(SessionUnavailable, match="source"):
        await service.submit(identity, request_id="one", text="When was the payment recorded?")


async def test_durable_sources_protect_the_unsynchronized_session_crash_window(tmp_path):
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    published_record(store)
    service = SessionService(
        reading_application(store),
        state_dir=tmp_path,
        authorize=always,
        validate_sources=published_source_validator(store),
    )
    identity = owner()
    service.create(identity)
    turn = await service.submit(identity, request_id="one", text="read")
    # Simulate a crash between durable runtime completion and session source sync.
    service._update_turn(turn.model_copy(update={"sources": ()}))
    store.withdraw(owner("publisher"), "case:1", expected_version=1)
    for read in (
        lambda: service.history(identity),
        lambda: service.inspect(identity, turn.id),
        lambda: service.events(identity, turn.id),
        lambda: service.get_turn(identity, turn.id),
    ):
        with pytest.raises(SessionUnavailable):
            read()
    with pytest.raises(SessionUnavailable):
        await service.resume(identity, turn.id)


@pytest.mark.parametrize(
    "content,metadata",
    [
        ("secret", {}),
        ("", {}),
        ("secret", {"source_empty": True}),
        ("", {"source_empty": True, "hidden": "text"}),
    ],
)
def test_governed_success_cannot_hide_missing_receipts(content, metadata):
    from forgeharness.domain.models import ToolEvidence
    from forgeharness.sdk.sources import governed_source_refs

    with pytest.raises(ValueError, match="missing source"):
        governed_source_refs(
            (ToolEvidence(name="read", ok=True, content=content, metadata=metadata),),
            frozenset({"read"}),
        )
    assert (
        governed_source_refs(
            (ToolEvidence(name="read", ok=True, content="", metadata={"source_empty": True}),),
            frozenset({"read"}),
        )
        == ()
    )

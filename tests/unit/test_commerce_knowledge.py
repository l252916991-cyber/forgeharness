"""FAQ knowledge publication, conflict detection and the handoff ticket ledger.

The FAQ cases publish the real synthetic corpus through a real ``RevisionStore``, so
the editor/reviewer/publisher separation and the idempotency guarantee are the ones a
host actually gets. The ticket cases cover the plan's ``create_handoff_ticket`` name,
its ``create_handoff`` alias, the status flow and the in-place column migration.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forgeharness.applications.commerce import (
    CommerceStore,
    FaqEntry,
    HandoffTicketTool,
    KnowledgeError,
    TicketInput,
    TicketTool,
    commerce_application,
    conflicting_faq_ids,
    faq_anchor,
    faq_resource_id,
    load_catalog,
    load_faq,
    load_faq_corpus,
    parse_faq,
    publish_faq,
    read_faq,
    seed_catalog,
    validate_faqs,
)
from forgeharness.sdk import (
    Actor,
    FinalAction,
    Harness,
    ModelRequest,
    ModelResult,
    RecordUnavailable,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
    ScopeError,
    ScriptedModel,
    SourceRef,
    ToolAction,
    ToolCall,
    ToolContext,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FAQ_ROOT = PROJECT_ROOT / "data" / "synthetic" / "commerce" / "faq"
CATALOG_PATH = PROJECT_ROOT / "data" / "synthetic" / "commerce" / "catalog.json"
SKU = "SYN-NB-001"
VERSION = 3


def context(
    *,
    run_id: str = "run-1",
    tenant: str = "shop-a",
    resources: tuple[str, ...] = (),
    roles: tuple[str, ...] = ("customer",),
    actor: str = "host-user",
) -> RunContext:
    return RunContext(
        application_id="commerce",
        application_version="1",
        policy_version="1",
        run_id=run_id,
        session_id="session-1",
        actor=Actor(id=actor, roles=roles),
        scope=ResourceScope(tenant_id=tenant, resources=resources),
        correlation_id="corr-1",
    )


def faq_document(faq_id: str = "faq-03") -> dict[str, Any]:
    return json.loads((FAQ_ROOT / f"{faq_id}.json").read_text(encoding="utf-8"))


def publication(
    tmp_path: Path, tenant: str = "shop-a"
) -> tuple[RevisionStore, tuple[FaqEntry, ...], tuple[RunContext, RunContext, RunContext]]:
    corpus = load_faq_corpus(FAQ_ROOT, tenant_id=tenant)
    resources = tuple(faq_resource_id(entry.id) for entry in corpus)
    records = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    roles = ("editor", "reviewer", "publisher")
    contexts = tuple(
        context(tenant=tenant, resources=resources, roles=(role,), actor=f"commerce-{role}")
        for role in roles
    )
    return records, corpus, (contexts[0], contexts[1], contexts[2])


def seeded(tmp_path: Path) -> CommerceStore:
    store = CommerceStore(tmp_path / "business.sqlite")
    seed_catalog(store, load_catalog(CATALOG_PATH))
    return store


def ticket_scope() -> tuple[str, ...]:
    return (f"product:{SKU}",)


def test_load_faq_corpus_parses_every_fixture() -> None:
    corpus = load_faq_corpus(FAQ_ROOT)
    assert len(corpus) == 24
    assert [entry.id for entry in corpus] == sorted(entry.id for entry in corpus)
    assert sum(entry.tenant_id == "shop-a" for entry in corpus) == 20
    assert len(load_faq_corpus(FAQ_ROOT, tenant_id="shop-b")) == 4
    first = corpus[0]
    assert (first.category, first.conflict_with) == ("shipping", "faq-02")
    assert first.sources[0].locator == "section:shipping-delivery-window"


def test_load_faq_requires_the_id_to_match_the_file_name(tmp_path: Path) -> None:
    path = tmp_path / "faq-01.json"
    path.write_text(json.dumps({**faq_document("faq-01"), "id": "faq-99"}), encoding="utf-8")
    with pytest.raises(KnowledgeError, match="file name"):
        load_faq(path)


def test_parse_faq_refuses_an_unknown_key() -> None:
    with pytest.raises(KnowledgeError, match="invalid FAQ document"):
        parse_faq({**faq_document(), "answer_url": "synthetic"})


def test_parse_faq_refuses_a_foreign_schema_version() -> None:
    with pytest.raises(KnowledgeError, match="schema_version"):
        parse_faq({**faq_document(), "schema_version": 2})


def test_parse_faq_refuses_a_self_declared_conflict() -> None:
    with pytest.raises(KnowledgeError, match="conflict with itself"):
        parse_faq({**faq_document(), "conflict_with": "faq-03"})


def test_validate_faqs_refuses_a_one_sided_conflict() -> None:
    corpus = load_faq_corpus(FAQ_ROOT, tenant_id="shop-a")
    one_sided = [
        entry.model_copy(update={"conflict_with": None}) if entry.id == "faq-02" else entry
        for entry in corpus
    ]
    with pytest.raises(KnowledgeError, match="both"):
        validate_faqs(one_sided)


def test_validate_faqs_refuses_an_unknown_conflict_target() -> None:
    corpus = load_faq_corpus(FAQ_ROOT, tenant_id="shop-a")
    dangling = [
        entry.model_copy(update={"conflict_with": "faq-24"}) if entry.id == "faq-01" else entry
        for entry in corpus
    ]
    with pytest.raises(KnowledgeError, match="unknown FAQ faq-24"):
        validate_faqs(dangling)


def test_conflicting_faq_ids_is_sorted_and_de_duplicated() -> None:
    corpus = load_faq_corpus(FAQ_ROOT)
    assert conflicting_faq_ids(corpus) == (
        ("faq-01", "faq-02"),
        ("faq-05", "faq-06"),
        ("faq-11", "faq-12"),
        ("faq-22", "faq-23"),
    )
    assert conflicting_faq_ids(reversed(corpus)) == conflicting_faq_ids(corpus)


def test_conflicting_faq_ids_ignores_a_one_sided_declaration() -> None:
    corpus = load_faq_corpus(FAQ_ROOT)
    one_sided = tuple(
        entry.model_copy(update={"conflict_with": None}) if entry.id == "faq-06" else entry
        for entry in corpus
    )
    assert conflicting_faq_ids(one_sided) == (
        ("faq-01", "faq-02"),
        ("faq-11", "faq-12"),
        ("faq-22", "faq-23"),
    )


def test_publish_faq_publishes_one_sourced_revision_per_entry(tmp_path: Path) -> None:
    store, corpus, (editor, reviewer, publisher) = publication(tmp_path)
    revisions = publish_faq(
        store=store, entries=corpus, editor=editor, reviewer=reviewer, publisher=publisher
    )
    assert len(revisions) == len(corpus) == 20
    first = revisions[0]
    assert (first.resource_id, first.version, first.state) == ("faq:faq-01", 1, "published")
    assert first.kind == "commerce_faq"
    assert [source.locator for source in first.sources] == ["section:shipping-delivery-window"]
    assert faq_anchor(first) == "faq:faq-01@1#section:shipping-delivery-window"
    parsed = SourceRef.from_anchor(faq_anchor(first))
    assert parsed is not None and parsed.resource_id == "faq:faq-01"


def test_publish_faq_is_idempotent_for_the_same_content(tmp_path: Path) -> None:
    store, corpus, (editor, reviewer, publisher) = publication(tmp_path)
    first = publish_faq(
        store=store, entries=corpus, editor=editor, reviewer=reviewer, publisher=publisher
    )
    agent = (editor, reviewer, publisher)
    second = publish_faq(
        store=store, entries=corpus, editor=agent[0], reviewer=agent[1], publisher=agent[2]
    )
    assert [revision.version for revision in second] == [revision.version for revision in first]
    assert [revision.digest for revision in second] == [revision.digest for revision in first]
    assert [event["operation"] for event in store.audit(editor, "faq:faq-01")] == [
        "draft.created",
        "approved",
        "published",
    ]


def test_publish_faq_requires_three_distinct_actors(tmp_path: Path) -> None:
    store, corpus, (editor, _reviewer, publisher) = publication(tmp_path)
    with pytest.raises(KnowledgeError, match="distinct"):
        publish_faq(
            store=store, entries=corpus, editor=editor, reviewer=editor, publisher=publisher
        )
    with pytest.raises(RecordUnavailable):
        store.read(editor, "faq:faq-01")


def test_publish_faq_refuses_a_reviewer_without_the_reviewer_role(tmp_path: Path) -> None:
    store, corpus, (editor, _reviewer, publisher) = publication(tmp_path)
    observer = context(
        resources=tuple(faq_resource_id(entry.id) for entry in corpus),
        roles=("reader",),
        actor="commerce-observer",
    )
    with pytest.raises(ScopeError):
        publish_faq(
            store=store, entries=corpus, editor=editor, reviewer=observer, publisher=publisher
        )
    assert store.read(editor, "faq:faq-01").state == "draft"


def test_publish_faq_refuses_another_tenants_entries(tmp_path: Path) -> None:
    store, _shop_a, (editor, reviewer, publisher) = publication(tmp_path)
    with pytest.raises(KnowledgeError, match="another tenant"):
        publish_faq(
            store=store,
            entries=load_faq_corpus(FAQ_ROOT),
            editor=editor,
            reviewer=reviewer,
            publisher=publisher,
        )


def test_read_faq_refuses_another_tenants_revision(tmp_path: Path) -> None:
    store, corpus, (editor, reviewer, publisher) = publication(tmp_path)
    publish_faq(store=store, entries=corpus, editor=editor, reviewer=reviewer, publisher=publisher)
    stranger = context(tenant="shop-b", resources=("faq:faq-01",), roles=("reader",))
    with pytest.raises(RecordUnavailable):
        read_faq(store=store, context=stranger, faq_id="faq-01")


def test_read_faq_requires_the_resource_in_scope(tmp_path: Path) -> None:
    store, corpus, (editor, reviewer, publisher) = publication(tmp_path)
    publish_faq(store=store, entries=corpus, editor=editor, reviewer=reviewer, publisher=publisher)
    unscoped = context(resources=("faq:faq-02",), roles=("reader",))
    with pytest.raises(ScopeError):
        read_faq(store=store, context=unscoped, faq_id="faq-01")
    scoped = context(resources=("faq:faq-01",), roles=("reader",), actor="commerce-reader")
    assert read_faq(store=store, context=scoped, faq_id="faq-01").version == 1


async def test_handoff_alias_and_plan_name_share_one_ledger(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    owner = context(resources=ticket_scope())
    payload = TicketInput(
        sku=SKU, request_id="handoff-1", reason="needs a person", expected_version=VERSION
    )
    first = await TicketTool(store).execute(
        payload, ToolContext(task_id="task-1", run_context=owner, idempotency_key="key-1")
    )
    second = await HandoffTicketTool(store).execute(
        payload, ToolContext(task_id="task-2", run_context=owner, idempotency_key="key-2")
    )
    assert first.content == second.content
    assert [row["id"] for row in store.list_tickets("shop-a")] == [first.content]


async def test_plan_handoff_name_refuses_a_conflicting_payload(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    owner = context(resources=ticket_scope())
    tool = HandoffTicketTool(store)
    payload = TicketInput(
        sku=SKU, request_id="handoff-1", reason="needs a person", expected_version=VERSION
    )
    await tool.execute(
        payload, ToolContext(task_id="task-1", run_context=owner, idempotency_key="key-1")
    )
    with pytest.raises(ValueError, match="business request"):
        await tool.execute(
            payload.model_copy(update={"reason": "changed"}),
            ToolContext(task_id="task-2", run_context=owner, idempotency_key="key-2"),
        )


async def test_plan_handoff_name_requires_a_stable_effect_key(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    owner = context(resources=ticket_scope())
    payload = TicketInput(
        sku=SKU, request_id="handoff-1", reason="needs a person", expected_version=VERSION
    )
    with pytest.raises(ValueError, match="effect key"):
        await HandoffTicketTool(store).execute(
            payload, ToolContext(task_id="task-1", run_context=owner)
        )


def test_general_ticket_is_idempotent_and_conflicts_on_changed_payload(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    first = store.general_ticket(
        key="general-key-1",
        tenant="shop-a",
        request_id="general-request",
        reason="no product matched",
    )
    assert (
        store.general_ticket(
            key="general-key-2",
            tenant="shop-a",
            request_id="general-request",
            reason="no product matched",
        )
        == first
    )
    with pytest.raises(ValueError, match="business request"):
        store.general_ticket(
            key="general-key-3",
            tenant="shop-a",
            request_id="general-request",
            reason="changed",
        )
    assert store.ticket_detail(first, tenant="shop-a")["sku"] == ""


def test_list_tickets_is_newest_first_and_tenant_scoped(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    first = store.ticket(
        key="key-1", tenant="shop-a", request_id="req-1", sku=SKU, reason="a", expected_version=3
    )
    second = store.ticket(
        key="key-2", tenant="shop-a", request_id="req-2", sku=SKU, reason="b", expected_version=3
    )
    store.ticket(
        key="key-3", tenant="shop-b", request_id="req-3", sku=SKU, reason="c", expected_version=2
    )
    rows = store.list_tickets("shop-a")
    assert [row["id"] for row in rows] == [second, first]
    assert {row["tenant"] for row in rows} == {"shop-a"}
    assert [row["id"] for row in store.list_tickets("shop-a", limit=1)] == [second]


def test_ticket_detail_reads_one_ticket_and_enforces_the_tenant(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    ticket = store.ticket(
        key="key-1", tenant="shop-a", request_id="req-1", sku=SKU, reason="a", expected_version=3
    )
    row = store.ticket_detail(ticket, tenant="shop-a")
    assert (row["id"], row["sku"], row["status"]) == (ticket, SKU, "open")
    with pytest.raises(ValueError, match="unavailable"):
        store.ticket_detail(ticket, tenant="shop-b")
    with pytest.raises(ValueError, match="unavailable"):
        store.ticket_detail("missing-ticket")


def test_ticket_status_follows_the_documented_forward_flow(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    ticket = store.ticket(
        key="key-1", tenant="shop-a", request_id="req-1", sku=SKU, reason="a", expected_version=3
    )
    assert store.set_ticket_status(ticket, "assigned", tenant="shop-a")["status"] == "assigned"
    assert store.set_ticket_status(ticket, "resolved", tenant="shop-a")["status"] == "resolved"
    assert store.ticket_detail(ticket, tenant="shop-a")["status"] == "resolved"


def test_open_ticket_can_be_cancelled(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    ticket = store.ticket(
        key="key-1", tenant="shop-a", request_id="req-1", sku=SKU, reason="a", expected_version=3
    )
    assert store.set_ticket_status(ticket, "cancelled")["status"] == "cancelled"
    assert store.ticket_detail(ticket)["status"] == "cancelled"


def test_unknown_ticket_status_is_refused(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    ticket = store.ticket(
        key="key-1", tenant="shop-a", request_id="req-1", sku=SKU, reason="a", expected_version=3
    )
    with pytest.raises(ValueError, match="unknown ticket status"):
        store.set_ticket_status(ticket, "escalated")


@pytest.mark.parametrize(
    ("current", "target"),
    [("open", "resolved"), ("open", "open"), ("assigned", "cancelled"), ("resolved", "assigned")],
)
def test_illegal_ticket_transitions_raise(tmp_path: Path, current: str, target: str) -> None:
    store = seeded(tmp_path)
    ticket = store.ticket(
        key="key-1", tenant="shop-a", request_id="req-1", sku=SKU, reason="a", expected_version=3
    )
    if current == "assigned":
        store.set_ticket_status(ticket, "assigned")
    elif current == "resolved":
        store.set_ticket_status(ticket, "assigned")
        store.set_ticket_status(ticket, "resolved")
    with pytest.raises(ValueError, match="illegal ticket transition"):
        store.set_ticket_status(ticket, target)
    assert store.ticket_detail(ticket)["status"] == current


def test_policy_requires_approval_for_each_handoff_name(tmp_path: Path) -> None:
    from forgeharness.sdk import AuthorizationRequest, PolicyDecisionType

    store = seeded(tmp_path)
    owner = context(resources=ticket_scope())
    supervisor = context(
        resources=ticket_scope(), roles=("customer", "supervisor"), actor="supervisor"
    )
    application = commerce_application(
        store=store,
        model_factory=lambda: ScriptedModel([ModelResult(action=FinalAction(content="ok"))]),
        catalog=load_catalog(CATALOG_PATH),
    )
    policy = application.policy
    assert isinstance(policy, CommercePolicyForTest) or policy is not None
    for name in ("create_handoff", "create_handoff_ticket"):
        spec = next(tool.spec for tool in application.tools if tool.spec.name == name)
        call = ToolCall(
            id="call-1",
            name=name,
            arguments={
                "sku": SKU,
                "request_id": "handoff-1",
                "reason": "needs a person",
                "expected_version": VERSION,
            },
        )
        decision = policy.evaluate(AuthorizationRequest(context=owner, spec=spec, call=call))
        assert decision.type is PolicyDecisionType.REQUIRE_APPROVAL
        assert policy.can_approve(requester=supervisor, owner=owner, call=call)
        assert not policy.can_approve(requester=owner, owner=owner, call=call)


class CommercePolicyForTest:
    """Placeholder type so a reader sees the policy contract is exercised."""


class HandoffModel:
    """Requests exactly one handoff under the plan's tool name."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        for message in reversed(request.messages):
            if message.role.value == "tool":
                return ModelResult(action=FinalAction(content=message.content or ""))
        return ModelResult(
            action=ToolAction(
                call=ToolCall(
                    id="handoff",
                    name="create_handoff_ticket",
                    arguments={
                        "sku": SKU,
                        "request_id": "handoff-1",
                        "reason": "customer asked for a person",
                        "expected_version": VERSION,
                    },
                )
            )
        )


async def test_plan_handoff_survives_two_runs_with_one_ticket(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    harness = Harness(
        commerce_application(
            store=store, model_factory=HandoffModel, catalog=load_catalog(CATALOG_PATH)
        ),
        state_dir=tmp_path / "state",
    )
    tickets = []
    for run_id in ("run-1", "run-2"):
        run = context(run_id=run_id, resources=ticket_scope())
        approver = context(
            run_id=run_id,
            resources=ticket_scope(),
            roles=("customer", "supervisor"),
            actor="supervisor",
        )
        assert (
            await harness.run(context=run, task="handoff")
        ).status is RunStatus.AWAITING_APPROVAL
        grant = harness.approve(context=run, approver=approver)
        result = await harness.resume(context=run, grant=grant, approver=approver)
        assert result.status is RunStatus.SUCCEEDED
        tickets.append(result.final_output)
    assert tickets[0] == tickets[1]
    assert len(store.list_tickets("shop-a")) == 1


def test_existing_ticket_ledger_migrates_the_status_column_in_place(tmp_path: Path) -> None:
    path = tmp_path / "business.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE tickets (id TEXT PRIMARY KEY, tenant TEXT, request_id TEXT, sku TEXT, "
            "reason TEXT, payload_hash TEXT, UNIQUE(tenant, request_id))"
        )
        connection.execute(
            "INSERT INTO tickets VALUES('t-1','shop-a','req-1',?, 'older','hash')", (SKU,)
        )
    store = CommerceStore(path)
    assert store.ticket_detail("t-1", tenant="shop-a")["status"] == "open"
    assert store.set_ticket_status("t-1", "assigned")["status"] == "assigned"
    assert [row["id"] for row in store.list_tickets("shop-a")] == ["t-1"]

"""Legal read-only pilot: page-level evidence, citations and pending-review notes (S3)."""

import asyncio
import json
from pathlib import Path

import pytest
from test_sdk import context

from forgeharness.applications.legal.adapter import CaseUnavailable, SyntheticCaseAdapter
from forgeharness.applications.legal.pilot import (
    LegalAnswer,
    LegalAnswerVerifier,
    case_qa_application,
    publish_case_pages,
)
from forgeharness.applications.legal.workflow import legal_review_workflow
from forgeharness.domain.models import Actor
from forgeharness.sdk import (
    ApplicationSpec,
    Citation,
    FinalAction,
    MessageRole,
    ModelRequest,
    ModelResult,
    PolicyDecisionType,
    RecordDraft,
    ResourceScope,
    RoleRecordPolicy,
    RunStatus,
    ScopeError,
    ScriptedModel,
    SourceAnchor,
    ToolAction,
    ToolCall,
    VerificationRequest,
)
from forgeharness.sdk.evidence import published_source_validator
from forgeharness.sdk.records import RevisionStore
from forgeharness.sdk.sessions import SessionService, SessionUnavailable
from forgeharness.sdk.sources import SourceRef
from forgeharness.sdk.workflows import WorkflowRunner

FIXTURES = Path(__file__).resolve().parents[2] / "data/synthetic/legal"


def owner(role: str, *, tenant: str = "firm-alpha", resources: tuple[str, ...] | None = None):
    case_id = "case-alpha" if tenant == "firm-alpha" else "case-beta"
    granted = resources or (
        f"case:{case_id}",
        *(f"case:{case_id}/page:{page}" for page in range(1, 7)),
        "draft:note-1",
    )
    return context(
        application_id="legal-case-qa",
        actor=Actor(id=role, roles=(role,)),
        scope=ResourceScope(tenant_id=tenant, resources=granted),
    )


def page_text(locator: str) -> str:
    """Map a ``page:N`` locator to the fixture file that holds it."""
    number = int(locator.split(":")[1])
    return (FIXTURES / "case-alpha" / f"page-{number:02d}.txt").read_text()


def questions() -> list[dict[str, object]]:
    return json.loads((FIXTURES / "case-alpha/questions.json").read_text())["questions"]


def answerable_question() -> dict[str, object]:
    return next(item for item in questions() if item["kind"] == "answerable")


@pytest.fixture
def adapter() -> SyntheticCaseAdapter:
    return SyntheticCaseAdapter(FIXTURES)


def published_case(tmp_path: Path) -> tuple[RevisionStore, SyntheticCaseAdapter]:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    adapter = SyntheticCaseAdapter(FIXTURES)
    publish_case_pages(
        adapter,
        store,
        reader=owner("editor"),
        reviewer=owner("reviewer"),
        publisher=owner("publisher"),
        case_id="case-alpha",
    )
    return store, adapter


class CitationModel:
    """Deterministic stand-in that cites the evidence it was just shown.

    It reads the tool observation the way a real model would and copies the span it
    found, so the test exercises the actual evidence flow rather than a hard-coded
    citation that could drift away from the fixtures.
    """

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
        observation = _last_tool_message(request)
        evidence = json.loads(observation)["evidence"][0]
        return ModelResult(
            action=FinalAction(
                content=LegalAnswer(
                    answer=f"Recorded evidence: {evidence['quote']}",
                    citations=(
                        Citation(
                            resource_id=evidence["resource_id"],
                            version=evidence["version"],
                            digest=evidence["digest"],
                            start=evidence["start"],
                            end=evidence["end"],
                            quote=evidence["quote"],
                        ),
                    ),
                ).model_dump_json()
            )
        )


def _last_tool_message(request: ModelRequest) -> str:
    for message in reversed(request.messages):
        if message.role is MessageRole.TOOL:
            return message.content or "{}"
    raise AssertionError("no tool observation was shown to the model")


def refuse_model() -> ScriptedModel:
    return ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(
                        id="search",
                        name="search_case",
                        arguments={"case_id": "case-alpha", "query": "bank account number"},
                    )
                )
            ),
            ModelResult(
                action=FinalAction(
                    content=LegalAnswer(
                        answer="The case material does not state this.",
                        citations=(),
                        insufficient_evidence=True,
                    ).model_dump_json()
                )
            ),
        ]
    )


def test_adapter_scopes_cases_to_tenant_and_scope(adapter: SyntheticCaseAdapter) -> None:
    assert adapter.cases(owner("reader")) == ("case-alpha",)
    assert adapter.cases(owner("reader", tenant="firm-beta")) == ("case-beta",)

    without_case = owner("reader").model_copy(
        update={"scope": ResourceScope(tenant_id="firm-alpha", resources=("case:other",))}
    )
    assert adapter.cases(without_case) == ()
    with pytest.raises(CaseUnavailable):
        adapter.case(without_case, "case-alpha")
    # A case in another tenant must look exactly like a missing one.
    with pytest.raises(CaseUnavailable):
        adapter.case(owner("reader"), "case-beta")


def test_adapter_search_returns_page_level_quotes(adapter: SyntheticCaseAdapter) -> None:
    expected = answerable_question()
    hits = adapter.search(owner("reader"), "case-alpha", str(expected["expected_quote"]))

    assert hits, "the fixture quote must be findable"
    assert hits[0].locator == expected["expected_page"]
    assert hits[0].source.locator == expected["expected_page"]
    assert hits[0].quote in page_text(str(expected["expected_page"]))


def test_adapter_rejects_unknown_pages_and_empty_queries(adapter: SyntheticCaseAdapter) -> None:
    with pytest.raises(CaseUnavailable):
        adapter.page(owner("reader"), "case-alpha", "page:99")
    with pytest.raises(ValueError, match="query"):
        adapter.search(owner("reader"), "case-alpha", "of")


async def test_answerable_question_is_cited_from_published_pages(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application = case_qa_application(adapter, store, model_factory=CitationModel)
    service = SessionService(
        application,
        state_dir=tmp_path / "sessions",
        authorize=lambda *_: True,
        validate_sources=published_source_validator(store),
    )
    service.create(owner("editor"))

    turn = await service.submit(owner("editor"), request_id="q1", text="What was transferred?")

    assert turn.status is RunStatus.SUCCEEDED, turn.error
    assert turn.output and "transferred" in turn.output
    assert SourceRef(resource_id="case:case-alpha/page:1", version="1", locator="page:1") in (
        service.get_turn(owner("editor"), turn.id).sources
    )


async def test_missing_evidence_is_refused_without_fabricating_a_source(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application = case_qa_application(adapter, store, model_factory=refuse_model)
    service = SessionService(
        application,
        state_dir=tmp_path / "sessions",
        authorize=lambda *_: True,
        validate_sources=published_source_validator(store),
    )
    service.create(owner("editor"))

    turn = await service.submit(
        owner("editor"), request_id="q2", text="What is the account number?"
    )

    assert turn.status is RunStatus.SUCCEEDED, turn.error
    assert turn.output and "does not state" in turn.output


async def test_uncited_factual_answer_fails_verification(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application = case_qa_application(
        adapter,
        store,
        model_factory=lambda: ScriptedModel(
            [
                ModelResult(
                    action=FinalAction(
                        content=LegalAnswer(
                            answer="The payment was definitely made."
                        ).model_dump_json()
                    )
                )
            ]
        ),
    )
    service = SessionService(
        application,
        state_dir=tmp_path / "sessions",
        authorize=lambda *_: True,
        validate_sources=published_source_validator(store),
    )
    service.create(owner("editor"))

    turn = await service.submit(owner("editor"), request_id="q3", text="Was the payment made?")

    # An uncited factual answer can never satisfy the contract, so the run must not
    # report success; the reason is recorded rather than silently accepted.
    assert turn.status is not RunStatus.SUCCEEDED
    reasons = [
        event["payload"].get("reason", "")
        for event in service.events(owner("editor"), turn.id)
        if event["type"] == "verification.rejected"
    ]
    assert reasons and all("cite" in reason for reason in reasons)


async def test_withdrawn_page_stops_the_answer_and_the_history(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application = case_qa_application(adapter, store, model_factory=CitationModel)
    service = SessionService(
        application,
        state_dir=tmp_path / "sessions",
        authorize=lambda *_: True,
        validate_sources=published_source_validator(store),
    )
    identity = owner("editor")
    service.create(identity)
    turn = await service.submit(identity, request_id="q4", text="What was transferred?")
    assert turn.status is RunStatus.SUCCEEDED

    store.withdraw(owner("publisher"), "case:case-alpha/page:1", expected_version=1)

    with pytest.raises((SessionUnavailable, ValueError)):
        service.history(identity)
    with pytest.raises((SessionUnavailable, ValueError)):
        service.inspect(identity, turn.id)


async def test_verifier_rejects_a_citation_to_a_superseded_version(tmp_path: Path) -> None:
    store, _ = published_case(tmp_path)
    verifier = LegalAnswerVerifier(store)
    stale = LegalAnswer(
        answer="Quoted from a version that no longer exists.",
        citations=(
            Citation(
                resource_id="case:alpha/page:1",
                version=99,
                digest="a" * 64,
                start=0,
                end=4,
                quote="Syn",
            ),
        ),
    )
    request = VerificationRequest(
        task_id="t",
        final=FinalAction(content=stale.model_dump_json()),
        workspace=None,
        tool_results=(),
        run_context=owner("editor"),
    )

    verdict = await verifier.verify(request)

    assert verdict.passed is False


def legal_note_identity(role: str):
    """The note workflow is its own application, so its records are namespaced to it."""
    return owner(role).model_copy(
        update={"application_id": "legal", "application_version": "1", "policy_version": "1"}
    )


def test_note_draft_stays_pending_until_a_human_reviews_it(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    editor = legal_note_identity("editor")
    store.save(
        editor,
        RecordDraft(
            resource_id="case:case-alpha/page:1",
            kind="case_evidence",
            content="18400.00 SCU was transferred on 2026-03-12",
            sources=(SourceAnchor(source="case:alpha", version="1", locator="page:1"),),
        ),
        expected_version=0,
        request_id="seed",
    )
    store.review(
        legal_note_identity("reviewer"), "case:case-alpha/page:1", expected_version=1, approve=True
    )
    store.publish(legal_note_identity("publisher"), "case:case-alpha/page:1", expected_version=1)

    workflow = legal_review_workflow(
        store=store,
        evidence_id="case:case-alpha/page:1",
        note_id="draft:note-1",
        business_request_id="note-1",
    )
    runner = WorkflowRunner(workflow, state_dir=tmp_path / "workflow")
    # A workflow run is bound to its own application identity.
    note_identity = owner("editor").model_copy(
        update={
            "application_id": workflow.id,
            "application_version": workflow.version,
            "policy_version": workflow.policy_version,
        }
    )

    result = asyncio.run(runner.run(context=note_identity, task="Draft a note"))

    # Writing the note is a model-visible write, so it suspends for an exact-action
    # approval by a separately authenticated supervisor.
    assert result.status is RunStatus.AWAITING_APPROVAL
    supervisor = note_identity.model_copy(
        update={"actor": Actor(id="supervisor", roles=("supervisor",))}
    )
    grant = runner.approve(context=note_identity, approver=supervisor)
    completed = asyncio.run(runner.resume(context=note_identity, grant=grant, approver=supervisor))

    assert completed.status is RunStatus.SUCCEEDED
    note = store.read(legal_note_identity("editor"), "draft:note-1")
    assert note.state == "draft", "a model draft is never auto-published"
    assert note.reviewer is None


def test_application_exposes_only_read_tools(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application: ApplicationSpec = case_qa_application(adapter, store, model_factory=CitationModel)

    assert {tool.spec.name for tool in application.tools} == {"search_case", "read_case_page"}
    assert [tool.spec.name for tool in application.tools] == ["search_case", "read_case_page"]
    assert all(tool.spec.governed_sources for tool in application.tools)


def test_publishing_pages_requires_distinct_roles(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    adapter = SyntheticCaseAdapter(FIXTURES)

    with pytest.raises(PermissionError):
        publish_case_pages(
            adapter,
            store,
            reader=owner("editor"),
            reviewer=owner("editor"),  # not a reviewer
            publisher=owner("publisher"),
            case_id="case-alpha",
        )


def test_adapter_refuses_broken_fixtures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fixture root"):
        SyntheticCaseAdapter(tmp_path / "missing")

    cases = tmp_path / "cases"
    cases.mkdir()
    # A directory without case.json is ignored, not treated as a case.
    (cases / "empty").mkdir()
    assert SyntheticCaseAdapter(cases).cases(owner("reader")) == ()

    broken = cases / "case-x"
    broken.mkdir()
    (broken / "case.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": "different-name",
                "tenant_id": "firm-alpha",
                "title": "Synthetic",
                "resources": [],
                "pages": [{"locator": "page:1", "file": "page-1.txt", "summary": ""}],
            }
        )
    )
    (broken / "page-1.txt").write_text("content")
    with pytest.raises(ValueError, match="does not match its case_id"):
        SyntheticCaseAdapter(cases)


def test_adapter_refuses_bad_locators_pages_and_empty_cases(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    case = cases / "case-y"
    case.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "case_id": "case-y",
        "tenant_id": "firm-alpha",
        "title": "Synthetic",
        "resources": [],
        "pages": [{"locator": "PAGE-1", "file": "page-1.txt", "summary": ""}],
    }
    (case / "case.json").write_text(json.dumps(manifest))
    (case / "page-1.txt").write_text("content")
    with pytest.raises(ValueError, match="invalid page locator"):
        SyntheticCaseAdapter(cases)

    manifest["pages"] = []
    (case / "case.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="no pages"):
        SyntheticCaseAdapter(cases)


def test_adapter_search_input_bounds(adapter: SyntheticCaseAdapter) -> None:
    with pytest.raises(ValueError, match="limit"):
        adapter.search(owner("reader"), "case-alpha", "transferred", limit=0)
    with pytest.raises(ValueError, match="query"):
        adapter.search(owner("reader"), "case-alpha", "")


def test_search_unavailable_case_is_refused(adapter: SyntheticCaseAdapter) -> None:
    with pytest.raises(CaseUnavailable):
        adapter.search(owner("reader"), "case-missing", "transferred")


async def test_search_reports_no_published_evidence(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    adapter = SyntheticCaseAdapter(FIXTURES)
    application = case_qa_application(adapter, store, model_factory=CitationModel)
    tool = application.tools[0]
    from forgeharness.sdk import ToolContext

    output = await tool.execute(
        tool.input_model.model_validate({"case_id": "case-alpha", "query": "transferred"}),
        ToolContext(task_id="t", run_context=owner("editor")),
    )

    assert output.ok is True
    assert output.content == "" and output.metadata == {"source_empty": True}


async def test_page_tool_reads_only_published_matching_pages(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application = case_qa_application(adapter, store, model_factory=CitationModel)
    page_tool = next(tool for tool in application.tools if tool.spec.name == "read_case_page")
    from forgeharness.sdk import ToolContext

    output = await page_tool.execute(
        page_tool.input_model.model_validate({"case_id": "case-alpha", "locator": "page:1"}),
        ToolContext(task_id="t", run_context=owner("editor")),
    )
    assert output.ok is True
    assert "page:1" in output.content
    assert output.receipt and output.receipt.evidence_refs

    store.withdraw(owner("publisher"), "case:case-alpha/page:6", expected_version=1)
    with pytest.raises(CaseUnavailable, match="not published"):
        await page_tool.execute(
            page_tool.input_model.model_validate({"case_id": "case-alpha", "locator": "page:6"}),
            ToolContext(task_id="t", run_context=owner("editor")),
        )


async def test_tools_require_a_trusted_context(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application = case_qa_application(adapter, store, model_factory=CitationModel)
    from forgeharness.sdk import ToolContext

    for tool in application.tools:
        with pytest.raises(ScopeError, match="trusted context"):
            await tool.execute(
                tool.input_model.model_validate(
                    {"case_id": "case-alpha", "query": "transferred"}
                    if tool.spec.name == "search_case"
                    else {"case_id": "case-alpha", "locator": "page:1"}
                ),
                ToolContext(task_id="t"),
            )


async def test_verifier_contract_violations_are_named(tmp_path: Path) -> None:
    store, _ = published_case(tmp_path)
    verifier = LegalAnswerVerifier(store)

    assert (
        await verifier.verify(
            VerificationRequest(task_id="t", final=FinalAction(content="{}"), workspace=None)
        )
    ).passed is False
    malformed = await verifier.verify(
        VerificationRequest(
            task_id="t",
            final=FinalAction(content="not json"),
            workspace=None,
            run_context=owner("editor"),
        )
    )
    assert malformed.passed is False and "contract" in malformed.reason
    contradictory = await verifier.verify(
        VerificationRequest(
            task_id="t",
            final=FinalAction(
                content=LegalAnswer(
                    answer="Nothing found.",
                    insufficient_evidence=True,
                    citations=(
                        Citation(
                            resource_id="case:case-alpha/page:1",
                            version=1,
                            digest="a" * 64,
                            start=0,
                            end=3,
                            quote="Syn",
                        ),
                    ),
                ).model_dump_json()
            ),
            workspace=None,
            run_context=owner("editor"),
        )
    )
    assert contradictory.passed is False and "must not carry citations" in contradictory.reason


def test_read_only_policy_denies_everything_else(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    application = case_qa_application(adapter, store, model_factory=CitationModel)
    policy = application.policy
    from forgeharness.sdk import AuthorizationRequest

    spec = application.tools[0].spec
    allowed = policy.evaluate(
        AuthorizationRequest(
            context=owner("editor"),
            spec=spec,
            call=ToolCall(
                id="c1", name="search_case", arguments={"case_id": "case-alpha", "query": "x"}
            ),
        )
    )
    denied = policy.evaluate(
        AuthorizationRequest(
            context=owner("editor"),
            spec=spec,
            call=ToolCall(id="c2", name="write_file", arguments={}),
        )
    )
    assert allowed.type is PolicyDecisionType.ALLOW
    assert denied.type is PolicyDecisionType.DENY
    assert policy.can_approve(requester=owner("editor"), owner=owner("editor"), call=None) is False


def test_case_application_requires_a_model_factory(tmp_path: Path) -> None:
    store, adapter = published_case(tmp_path)
    with pytest.raises(ValueError, match="model factory"):
        case_qa_application(adapter, store, model_factory=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("violation", ["duplicate", "escape", "manifest_symlink"])
def test_fixture_adapter_refuses_ambiguous_or_escaped_pages(tmp_path, violation):
    import shutil

    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, root)
    manifest = root / "case-alpha/case.json"
    value = json.loads(manifest.read_text())
    if violation == "duplicate":
        value["pages"].append(value["pages"][0])
    elif violation == "escape":
        value["pages"][0]["file"] = "../../private.txt"
        (tmp_path / "private.txt").write_text("must not be read")
    else:
        external = tmp_path / "external.json"
        external.write_text(manifest.read_text())
        manifest.unlink()
        manifest.symlink_to(external)
    if violation != "manifest_symlink":
        manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match=r"duplicate|outside"):
        SyntheticCaseAdapter(root)


def test_adapter_filters_page_grants_before_search(adapter):
    reader = owner("reader", resources=("case:case-alpha",))
    assert adapter.search(reader, "case-alpha", "transferred") == ()
    with pytest.raises(CaseUnavailable):
        adapter.page(reader, "case-alpha", "page:1")


async def test_search_rejects_changed_page_even_when_quote_still_exists(tmp_path):
    from forgeharness.applications.legal.pilot import CaseSearchInput, CaseSearchTool
    from forgeharness.sdk import ToolContext

    store, adapter = published_case(tmp_path)
    page = adapter.page(owner("reader"), "case-alpha", "page:1")
    adapter._pages["case-alpha"]["page:1"] = page.model_copy(
        update={"content": page.content + "\nChanged context."}
    )
    result = await CaseSearchTool(adapter, store).execute(
        CaseSearchInput(case_id="case-alpha", query="transferred"),
        ToolContext(task_id="test", workspace=None, run_context=owner("reader")),
    )
    if result.content:
        assert all(
            item["resource_id"] != "case:case-alpha/page:1"
            for item in json.loads(result.content)["evidence"]
        )
    else:
        assert result.metadata == {"source_empty": True}

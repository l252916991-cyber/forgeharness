# ruff: noqa: RUF001
# Chinese legal fixtures intentionally use full-width punctuation.
"""Read-only LexVault-shaped DAG: parallel specialists, Critic, citation check."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from forgeharness.applications.legal.adapter import CaseUnavailable
from forgeharness.applications.legal.lexvault import (
    BundleCaseAdapter,
    BundleCitationVerifier,
    BundlePage,
    CaseBundle,
    LexVaultDagOrchestrator,
    ReadOnlyDagPolicy,
    lexvault_dag_application,
)
from forgeharness.applications.legal.lexvault.bundle import text_digest
from forgeharness.sdk import (
    Actor,
    AuthorizationRequest,
    FinalAction,
    Harness,
    ModelResult,
    PolicyDecisionType,
    ResourceScope,
    RiskLevel,
    RunContext,
    RunStatus,
    ScriptedModel,
    ToolAction,
    ToolCall,
    ToolSpec,
    VerificationRequest,
    verify_trace,
)


def bundle() -> CaseBundle:
    return CaseBundle(
        tenant_id="tenant-a",
        case_id="case-alpha",
        title="示例案件",
        version="1",
        pages=(
            BundlePage(
                page_no=1,
                text="2024年1月，甲公司向乙公司支付资金500万元，用于设备采购。",
                summary="付款",
            ),
            BundlePage(
                page_no=2,
                text="证据清单：付款凭证、合同附件、银行流水。",
                summary="证据",
            ),
            BundlePage(
                page_no=3,
                text="乙公司否认收到上述款项，称未签署合同。",
                summary="否认",
            ),
        ),
    )


def context(*, tenant: str = "tenant-a", resources: tuple[str, ...] | None = None) -> RunContext:
    return RunContext(
        application_id="lexvault-dag",
        application_version="1",
        policy_version="1",
        run_id="run-1",
        session_id="session-1",
        actor=Actor(id="lawyer", roles=("use",)),
        scope=ResourceScope(
            tenant_id=tenant,
            resources=resources
            or (
                "case:case-alpha",
                "case:case-alpha/page:1",
                "case:case-alpha/page:2",
                "case:case-alpha/page:3",
            ),
        ),
        correlation_id="correlation-1",
    )


def app_with(model_result: ModelResult, case_bundle: CaseBundle):
    adapter = BundleCaseAdapter(case_bundle)
    verifier = BundleCitationVerifier(case_bundle)
    application = lexvault_dag_application(
        model_factory=lambda: ScriptedModel([model_result]),
        verifier=verifier,
    )
    return adapter, application


async def run_dag(
    tmp_path: Path,
    model_result: ModelResult,
    *,
    case_bundle: CaseBundle | None = None,
    run_context: RunContext | None = None,
):
    selected = case_bundle or bundle()
    adapter, application = app_with(model_result, selected)
    harness = Harness(
        application,
        state_dir=tmp_path,
        orchestrator=LexVaultDagOrchestrator(adapter, case_id=selected.case_id),
    )
    result = await harness.run(
        context=run_context or context(),
        task="梳理资金流向和矛盾",
    )
    return result, harness


async def test_parallel_specialists_then_critic_succeeds(tmp_path: Path) -> None:
    result, harness = await run_dag(
        tmp_path,
        ModelResult(
            action=FinalAction(content="甲公司支付500万元[page:1]；乙公司否认收款[page:3]。")
        ),
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output is not None
    assert "[page:1]" in result.final_output
    assert result.usage.steps == 1
    assert harness.inspect(context()).status is RunStatus.SUCCEEDED

    trace = harness.trace_path(context())
    assert verify_trace(trace).valid
    events = [json.loads(line)["event"] for line in trace.read_text().splitlines()]
    nodes = {event["payload"]["node"] for event in events if event["type"] == "dag.node.completed"}
    assert nodes == {"facts", "evidence", "contradiction"}
    assert any(
        event["type"] == "model.request" and event["payload"].get("phase") == "critic"
        for event in events
    )


async def test_citation_outside_bundle_is_rejected(tmp_path: Path) -> None:
    result, harness = await run_dag(
        tmp_path,
        ModelResult(action=FinalAction(content="结论[page:99]。")),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "outside the bundle" in result.error
    assert "verification.rejected" in harness.trace_path(context()).read_text()


async def test_factual_answer_without_citation_is_rejected(tmp_path: Path) -> None:
    result, _ = await run_dag(
        tmp_path,
        ModelResult(action=FinalAction(content="甲公司支付了款项。")),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "must cite" in result.error


async def test_declared_insufficiency_is_accepted_without_citation(tmp_path: Path) -> None:
    result, _ = await run_dag(
        tmp_path,
        ModelResult(action=FinalAction(content="材料不足，无法判断。")),
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "材料不足，无法判断。"


def test_bundle_rejects_duplicate_pages() -> None:
    with pytest.raises(ValidationError, match="duplicate page"):
        CaseBundle(
            tenant_id="tenant-a",
            case_id="case-alpha",
            title="重复页",
            pages=(
                BundlePage(page_no=1, text="第一页"),
                BundlePage(page_no=1, text="重复"),
            ),
        )


def test_adapter_enforces_tenant_and_page_scope() -> None:
    adapter = BundleCaseAdapter(bundle())

    with pytest.raises(CaseUnavailable):
        adapter.case(context(tenant="tenant-b"), "case-alpha")

    with pytest.raises(CaseUnavailable):
        adapter.page(
            context(resources=("case:case-alpha", "case:case-alpha/page:1")),
            "case-alpha",
            "page:3",
        )


def test_bundle_search_returns_exact_span() -> None:
    adapter = BundleCaseAdapter(bundle())

    hit = adapter.search(context(), "case-alpha", "资金", limit=1)[0]

    page = adapter.bundle.page_by_no(1)
    assert page is not None
    assert hit.quote == page.text[hit.start : hit.end]
    assert hit.locator == "page:1"


def test_page_drafts_are_source_bound() -> None:
    selected = bundle()
    adapter = BundleCaseAdapter(selected)

    drafts = adapter.page_drafts(context(), "case-alpha")

    assert [resource for resource, _ in drafts] == [
        "case:case-alpha/page:1",
        "case:case-alpha/page:2",
        "case:case-alpha/page:3",
    ]
    _, first = drafts[0]
    assert first.content == selected.pages[0].text
    assert first.sources[0].locator == "page:1"


async def test_exact_quote_citation_passes(tmp_path: Path) -> None:
    result, _ = await run_dag(
        tmp_path,
        ModelResult(action=FinalAction(content="甲公司付款[page:1]「支付资金500万元」。")),
    )

    assert result.status is RunStatus.SUCCEEDED


async def test_paraphrased_quote_is_rejected(tmp_path: Path) -> None:
    result, _ = await run_dag(
        tmp_path,
        ModelResult(action=FinalAction(content="甲公司付款[page:1]「支付了五百万元」。")),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "exact span" in result.error


async def test_strict_mode_requires_a_quote() -> None:
    verifier = BundleCitationVerifier(bundle(), strict_quotes=True)

    bare = await verifier.verify(
        VerificationRequest(
            task_id="t",
            final=FinalAction(content="结论[page:1]。"),
            workspace=None,
        )
    )
    quoted = await verifier.verify(
        VerificationRequest(
            task_id="t",
            final=FinalAction(content="结论[page:1]「支付资金500万元」。"),
            workspace=None,
        )
    )

    assert not bare.passed
    assert "strict" in bare.reason
    assert quoted.passed


async def test_empty_quote_is_rejected() -> None:
    verifier = BundleCitationVerifier(bundle())

    result = await verifier.verify(
        VerificationRequest(
            task_id="t",
            final=FinalAction(content="结论[page:1]「」。"),
            workspace=None,
        )
    )

    assert not result.passed
    assert "empty quote" in result.reason


async def test_tampered_page_text_fails_digest_check() -> None:
    selected = bundle()
    tampered_page = selected.pages[0].model_copy(update={"text": "篡改后的文本"})
    tampered = selected.model_copy(update={"pages": (tampered_page, *selected.pages[1:])})
    verifier = BundleCitationVerifier(tampered)

    result = await verifier.verify(
        VerificationRequest(
            task_id="t",
            final=FinalAction(content="结论[page:1]。"),
            workspace=None,
        )
    )

    assert not result.passed
    assert "digest" in result.reason


def test_page_digest_is_derived_and_enforced() -> None:
    page = BundlePage(page_no=1, text="原文")
    assert page.text_sha256 == text_digest("原文")

    with pytest.raises(ValidationError, match="does not match"):
        BundlePage(page_no=1, text="原文", text_sha256="0" * 64)


def test_snapshot_digest_tracks_page_content() -> None:
    first = bundle()
    same = bundle()
    changed_page = first.pages[0].model_copy(
        update={"text": "不同的文本", "text_sha256": text_digest("不同的文本")}
    )
    changed = first.model_copy(update={"pages": (changed_page, *first.pages[1:])})

    assert first.snapshot_digest == same.snapshot_digest
    assert changed.snapshot_digest != first.snapshot_digest


def test_adapter_authorization_surface() -> None:
    adapter = BundleCaseAdapter(bundle())

    assert adapter.cases(context()) == ("case-alpha",)
    assert adapter.cases(context(tenant="tenant-b")) == ()
    assert adapter.resources(context(), "case-alpha") == (
        "case:case-alpha",
        "case:case-alpha/page:1",
        "case:case-alpha/page:2",
        "case:case-alpha/page:3",
    )
    limited = context(resources=("case:case-alpha", "case:case-alpha/page:2"))
    assert [locator for locator, _ in adapter.iter_pages(limited, "case-alpha")] == ["page:2"]


def test_adapter_search_input_bounds() -> None:
    adapter = BundleCaseAdapter(bundle())

    with pytest.raises(ValueError, match="limit"):
        adapter.search(context(), "case-alpha", "资金", limit=0)
    with pytest.raises(ValueError, match="limit"):
        adapter.search(context(), "case-alpha", "资金", limit=51)
    with pytest.raises(ValueError, match="bounded"):
        adapter.search(context(), "case-alpha", "   ")
    with pytest.raises(ValueError, match="bounded"):
        adapter.search(context(), "case-alpha", "资" * 501)
    with pytest.raises(ValueError, match="no searchable terms"):
        adapter.search(context(), "case-alpha", "资")


def test_search_and_drafts_skip_unauthorized_pages() -> None:
    adapter = BundleCaseAdapter(bundle())
    limited = context(resources=("case:case-alpha", "case:case-alpha/page:1"))

    # "证据" only appears on page 2, which this binding may not read.
    assert adapter.search(limited, "case-alpha", "证据", limit=5) == ()
    drafts = adapter.page_drafts(limited, "case-alpha")
    assert [resource for resource, _ in drafts] == ["case:case-alpha/page:1"]


def test_read_only_dag_policy_allows_only_reads() -> None:
    policy = ReadOnlyDagPolicy()
    read = ToolSpec(name="r", description="d", input_schema={}, risk=RiskLevel.READ)
    write = ToolSpec(name="w", description="d", input_schema={}, risk=RiskLevel.WRITE)

    allow = policy.evaluate(
        AuthorizationRequest(context=context(), spec=read, call=ToolCall(id="c", name="r"))
    )
    deny = policy.evaluate(
        AuthorizationRequest(context=context(), spec=write, call=ToolCall(id="c", name="w"))
    )

    assert allow.type is PolicyDecisionType.ALLOW
    assert deny.type is PolicyDecisionType.DENY
    assert policy.can_approve(requester=context(), owner=context(), call=None) is False


async def test_verifier_rejects_empty_answer() -> None:
    verifier = BundleCitationVerifier(bundle())

    result = await verifier.verify(
        VerificationRequest(
            task_id="t",
            final=FinalAction(content="   "),
            workspace=None,
        )
    )

    assert not result.passed
    assert result.reason == "answer is empty"


async def test_non_final_critic_action_fails_closed(tmp_path: Path) -> None:
    result, _ = await run_dag(
        tmp_path,
        ModelResult(action=ToolAction(call=ToolCall(id="x", name="nope", arguments={}))),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "non-final action" in result.error

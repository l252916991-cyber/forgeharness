"""Application contract conformance for legal and commerce reference apps."""

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from forgeharness.applications.commerce import (
    CommerceStore,
    commerce_application,
    commerce_readonly_application,
)
from forgeharness.applications.legal.adapter import SyntheticCaseAdapter
from forgeharness.applications.legal.lexvault import (
    BundleCitationVerifier,
    BundlePage,
    CaseBundle,
    lexvault_dag_application,
)
from forgeharness.applications.legal.pilot import case_qa_application, publish_case_pages
from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    FinalAction,
    Harness,
    ModelResult,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    ScopeError,
    ScriptedModel,
    ToolSpec,
)

FIXTURES = Path(__file__).resolve().parents[2] / "data/synthetic/legal"


def legal_identity(role: str) -> RunContext:
    return RunContext(
        application_id="legal-case-qa",
        application_version="1",
        policy_version="1",
        run_id=f"conformance-{role}",
        session_id="conformance",
        actor=Actor(id=role, roles=(role,)),
        scope=ResourceScope(
            tenant_id="firm-alpha",
            resources=(
                "case:case-alpha",
                *(f"case:case-alpha/page:{page}" for page in range(1, 7)),
            ),
        ),
        correlation_id="conformance",
    )


def legal_pilot(tmp_path: Path) -> ApplicationSpec:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    adapter = SyntheticCaseAdapter(FIXTURES)
    publish_case_pages(
        adapter,
        store,
        reader=legal_identity("editor"),
        reviewer=legal_identity("reviewer"),
        publisher=legal_identity("publisher"),
        case_id="case-alpha",
    )
    return case_qa_application(
        adapter,
        store,
        model_factory=lambda: ScriptedModel([ModelResult(action=FinalAction(content="ok"))]),
    )


def legal_dag(tmp_path: Path) -> ApplicationSpec:
    bundle = CaseBundle(
        tenant_id="tenant-a",
        case_id="case-dag",
        title="Conformance case",
        pages=(BundlePage(page_no=1, text="甲公司支付资金100万元。", summary="付款"),),
    )
    return lexvault_dag_application(
        model_factory=lambda: ScriptedModel(
            [ModelResult(action=FinalAction(content="材料不足[page:1]。"))]
        ),
        verifier=BundleCitationVerifier(bundle),
    )


def commerce(tmp_path: Path) -> ApplicationSpec:
    store = CommerceStore(tmp_path / "commerce.sqlite")
    return commerce_application(
        store=store,
        model_factory=lambda: ScriptedModel([ModelResult(action=FinalAction(content="ok"))]),
    )


def commerce_readonly(tmp_path: Path) -> ApplicationSpec:
    store = CommerceStore(tmp_path / "commerce-readonly.sqlite")
    return commerce_readonly_application(
        store=store,
        model_factory=lambda: ScriptedModel([ModelResult(action=FinalAction(content="ok"))]),
        authorize=lambda _: True,
    )


FACTORIES: tuple[Callable[[Path], ApplicationSpec], ...] = (
    legal_pilot,
    legal_dag,
    commerce,
    commerce_readonly,
)


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda item: item.__name__)
def test_application_spec_contract(
    tmp_path: Path, factory: Callable[[Path], ApplicationSpec]
) -> None:
    app = factory(tmp_path)

    assert isinstance(app, ApplicationSpec)
    assert app.id and app.version and app.policy_version
    assert callable(app.policy.evaluate)
    assert callable(app.policy.can_approve)
    assert callable(app.verifier.verify)
    assert app.budget.max_steps >= 1
    assert isinstance(app.instructions, tuple)
    assert isinstance(app.context_providers, tuple)

    names = [tool.spec.name for tool in app.tools]
    assert len(names) == len(set(names)), f"duplicate tool names in {app.id}"

    for tool in app.tools:
        assert isinstance(tool.spec, ToolSpec)
        assert tool.spec.name
        assert tool.spec.description
        assert tool.spec.input_schema
        assert isinstance(tool.input_model, type)
        assert issubclass(tool.input_model, BaseModel)
        assert callable(tool.execute)


def test_tool_order_is_stable(tmp_path: Path) -> None:
    expected = {
        "legal_pilot": ["search_case", "read_case_page"],
        "legal_dag": [],
        "commerce": [
            "product_lookup",
            "search_products",
            "get_price_stock",
            "create_handoff",
            "create_handoff_ticket",
        ],
        "commerce_readonly": ["product_lookup"],
    }
    applications = {
        "legal_pilot": legal_pilot(tmp_path / "legal"),
        "legal_dag": legal_dag(tmp_path / "dag"),
        "commerce": commerce(tmp_path / "commerce"),
        "commerce_readonly": commerce_readonly(tmp_path / "commerce-readonly"),
    }

    for name, app in applications.items():
        assert [tool.spec.name for tool in app.tools] == expected[name]


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda item: item.__name__)
async def test_wrong_application_binding_is_rejected(
    tmp_path: Path, factory: Callable[[Path], ApplicationSpec]
) -> None:
    app = factory(tmp_path)
    harness = Harness(app, state_dir=tmp_path / "state")
    wrong = RunContext(
        application_id="another-application",
        application_version="1",
        policy_version="1",
        run_id="wrong-binding",
        session_id="conformance",
        actor=Actor(id="user", roles=("use",)),
        scope=ResourceScope(tenant_id="tenant-a", resources=()),
        correlation_id="conformance",
    )

    with pytest.raises(ScopeError, match="application or policy version mismatch"):
        await harness.run(context=wrong, task="should not run")

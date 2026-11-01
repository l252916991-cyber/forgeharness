"""Catalogue contract and the two governed product reads.

Every case drives the real synthetic fixture (mutated copies are written into
``tmp_path``), so the strict schema, the version-guarded seeding and the scoped
deterministic search are exercised exactly as the reference application uses them.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forgeharness.applications.commerce import (
    CatalogError,
    CatalogProduct,
    CommercePolicy,
    CommerceStore,
    CommerceVerifier,
    GetPriceStockTool,
    PriceStockInput,
    ProductSearchInput,
    SearchProductsTool,
    load_catalog,
    search_catalog,
    seed_catalog,
)
from forgeharness.sdk import (
    Actor,
    AuthorizationRequest,
    FinalAction,
    PolicyDecisionType,
    ResourceScope,
    RunContext,
    SourceRef,
    ToolCall,
    ToolContext,
    ToolEvidence,
    ToolSpec,
    VerificationRequest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = PROJECT_ROOT / "data" / "synthetic" / "commerce" / "catalog.json"
SKU = "SYN-NB-001"
VERSION = 3


def context(
    *,
    tenant: str = "shop-a",
    resources: tuple[str, ...] = (),
    roles: tuple[str, ...] = ("customer",),
) -> RunContext:
    return RunContext(
        application_id="commerce",
        application_version="1",
        policy_version="1",
        run_id="run-1",
        session_id="session-1",
        actor=Actor(id="host-user", roles=roles),
        scope=ResourceScope(tenant_id=tenant, resources=resources),
        correlation_id="corr-1",
    )


def tool_context(owner: RunContext) -> ToolContext:
    return ToolContext(task_id="task-1", run_context=owner)


def document() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def rewritten(tmp_path: Path, products: list[dict[str, Any]]) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({**document(), "products": products}), encoding="utf-8")
    return path


def seeded(tmp_path: Path) -> tuple[CommerceStore, tuple[CatalogProduct, ...]]:
    store = CommerceStore(tmp_path / "business.sqlite")
    products = load_catalog(CATALOG_PATH)
    assert seed_catalog(store, products) == len(products)
    return store, products


def snapshot(store: CommerceStore) -> tuple[tuple[object, ...], ...]:
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT tenant, sku, name, price_cents, stock, version FROM products "
            "ORDER BY tenant, sku"
        ).fetchall()
    return tuple(tuple(row) for row in rows)


def first_row() -> dict[str, Any]:
    return document()["products"][0]


def test_load_catalog_parses_the_synthetic_fixture() -> None:
    products = load_catalog(CATALOG_PATH)
    assert len(products) == 36
    assert sum(product.tenant_id == "shop-a" for product in products) == 30
    first = products[0]
    assert (first.tenant_id, first.sku, first.version) == ("shop-a", SKU, VERSION)
    assert first.price_cents == 1299 and first.stock == 12 and first.currency == "CNY"
    assert first.attributes["pages"] == "80"
    assert (first.source.source, first.source.version, first.source.locator) == (
        "shop-a-catalog",
        "2026-10-01",
        "row:1",
    )


def test_load_catalog_refuses_a_duplicate_tenant_sku(tmp_path: Path) -> None:
    rows = document()["products"]
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog(rewritten(tmp_path, [*rows, dict(rows[0])]))


def test_load_catalog_refuses_a_nonpositive_price(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="price_cents"):
        load_catalog(rewritten(tmp_path, [{**first_row(), "price_cents": 0}]))


def test_load_catalog_refuses_a_negative_stock(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="stock"):
        load_catalog(rewritten(tmp_path, [{**first_row(), "stock": -1}]))


def test_load_catalog_refuses_a_version_below_one(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="version"):
        load_catalog(rewritten(tmp_path, [{**first_row(), "version": 0}]))


def test_load_catalog_refuses_an_empty_currency(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="currency"):
        load_catalog(rewritten(tmp_path, [{**first_row(), "currency": ""}]))


def test_load_catalog_refuses_a_locator_that_cannot_be_an_anchor(tmp_path: Path) -> None:
    source = {**first_row()["source"], "locator": "row#1"}
    with pytest.raises(CatalogError, match="locator"):
        load_catalog(rewritten(tmp_path, [{**first_row(), "source": source}]))


def test_load_catalog_refuses_an_undeclared_tenant(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="undeclared"):
        load_catalog(rewritten(tmp_path, [{**first_row(), "tenant_id": "shop-c"}]))


def test_seed_catalog_is_idempotent_across_repeated_runs(tmp_path: Path) -> None:
    store = CommerceStore(tmp_path / "business.sqlite")
    products = load_catalog(CATALOG_PATH)
    assert seed_catalog(store, products) == 36
    before = snapshot(store)
    assert seed_catalog(store, products) == 0
    assert snapshot(store) == before
    assert len(before) == 36


def test_seed_catalog_refuses_a_stale_version_without_touching_the_row(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    stored = store.product("shop-a", SKU)
    stale = CatalogProduct.model_validate(
        {**products[0].model_dump(), "version": VERSION - 1, "price_cents": 1}
    )
    with pytest.raises(CatalogError, match="stale"):
        seed_catalog(store, [stale])
    assert store.product("shop-a", SKU) == stored


def test_seed_catalog_refuses_one_version_naming_different_content(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    stored = store.product("shop-a", SKU)
    changed = CatalogProduct.model_validate({**products[0].model_dump(), "price_cents": 1})
    with pytest.raises(CatalogError, match="different content"):
        seed_catalog(store, [changed])
    assert store.product("shop-a", SKU) == stored


def test_search_catalog_excludes_other_tenants_and_unscoped_rows() -> None:
    # The fixture reuses this sku for two different products, one per tenant.
    products = load_catalog(CATALOG_PATH)
    shop_b = context(tenant="shop-b", resources=(f"product:{SKU}",))
    hits = search_catalog(products, shop_b, "clamp")
    assert [(hit.tenant_id, hit.sku, hit.price_cents) for hit in hits] == [("shop-b", SKU, 4580)]
    shop_a = context(resources=(f"product:{SKU}",))
    assert search_catalog(products, shop_a, "clamp") == ()
    assert search_catalog(products, context(resources=()), "notebook") == ()


def test_search_catalog_matches_name_category_and_attributes_case_insensitively() -> None:
    products = load_catalog(CATALOG_PATH)
    scope = tuple(f"product:{product.sku}" for product in products if product.tenant_id == "shop-a")
    owner = context(resources=scope)
    stationery = search_catalog(products, owner, "STATIONERY", limit=50)
    assert stationery and {product.category for product in stationery} == {"stationery"}
    assert {product.sku for product in search_catalog(products, owner, "a5", limit=50)} == {
        "SYN-NB-001",
        "SYN-NB-002",
    }
    assert [product.sku for product in search_catalog(products, owner, "0.5 MM")] == ["SYN-PN-003"]


def test_search_catalog_orders_products_sharing_a_name_by_sku() -> None:
    products = load_catalog(CATALOG_PATH)
    scope = tuple(f"product:{product.sku}" for product in products if product.tenant_id == "shop-a")
    hits = search_catalog(products, context(resources=scope), "Synthetic storage crate 20 litres")
    assert [product.sku for product in hits] == ["SYN-KT-401", "SYN-KT-402"]


def test_search_catalog_refuses_a_blank_query() -> None:
    products = load_catalog(CATALOG_PATH)
    owner = context(resources=(f"product:{SKU}",))
    with pytest.raises(ValueError, match="must not be empty"):
        search_catalog(products, owner, "   ")


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_search_catalog_refuses_a_limit_outside_the_documented_range(limit: int) -> None:
    products = load_catalog(CATALOG_PATH)
    owner = context(resources=(f"product:{SKU}",))
    with pytest.raises(ValueError, match="limit"):
        search_catalog(products, owner, "notebook", limit=limit)


async def test_search_products_returns_scoped_citation_ready_entries(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=(f"product:{SKU}",))
    output = await SearchProductsTool(store, products).execute(
        ProductSearchInput(query="notebook"), tool_context(owner)
    )
    entries = json.loads(output.content)
    # SYN-NB-002 also matches the query but is not in the caller's scope.
    assert [entry["sku"] for entry in entries] == [SKU]
    entry = entries[0]
    assert entry["resource_id"] == f"product:{SKU}"
    assert (entry["version"], entry["currency"], entry["locator"]) == (VERSION, "CNY", "row:1")
    assert entry["price_cents"] == 1299 and entry["stock"] == 12
    assert len(entry["digest"]) == 64


async def test_search_products_receipt_carries_parseable_governed_anchors(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=(f"product:{SKU}",))
    output = await SearchProductsTool(store, products).execute(
        ProductSearchInput(query="notebook"), tool_context(owner)
    )
    assert output.receipt is not None
    assert output.receipt.evidence_refs == (f"product:{SKU}@{VERSION}#row:1",)
    parsed = SourceRef.from_anchor(output.receipt.evidence_refs[0])
    assert parsed is not None
    assert (parsed.resource_id, parsed.version, parsed.locator) == (f"product:{SKU}", "3", "row:1")


async def test_search_products_without_a_hit_states_no_source(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=(f"product:{SKU}",))
    output = await SearchProductsTool(store, products).execute(
        ProductSearchInput(query="nothing in this catalogue matches"), tool_context(owner)
    )
    assert output.ok and output.content == ""
    assert output.metadata == {"source_empty": True}
    assert output.receipt is None


async def test_search_products_result_is_revalidated_against_the_live_ledger(
    tmp_path: Path,
) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=(f"product:{SKU}",))
    output = await SearchProductsTool(store, products).execute(
        ProductSearchInput(query="notebook"), tool_context(owner)
    )
    assert output.receipt is not None
    request = VerificationRequest(
        task_id="run-1",
        workspace=None,
        run_context=owner,
        final=FinalAction(content=output.content),
        tool_results=(
            ToolEvidence(
                name="search_products",
                ok=True,
                content=output.content,
                metadata=output.metadata,
                receipt=output.receipt,
            ),
        ),
    )
    verifier = CommerceVerifier(store)
    assert (await verifier.verify(request)).passed
    store.seed(
        tenant="shop-a",
        sku=SKU,
        name="Synthetic notebook A5 80 pages",
        price_cents=1299,
        stock=12,
        version=VERSION + 1,
    )
    assert not (await verifier.verify(request)).passed


async def test_get_price_stock_reports_the_injected_query_timestamp(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=(f"product:{SKU}",))
    fixed = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    tool = GetPriceStockTool(store, products, clock=lambda: fixed)
    output = await tool.execute(PriceStockInput(sku=SKU), tool_context(owner))
    payload = json.loads(output.content)
    assert payload == {
        "sku": SKU,
        "price_cents": 1299,
        "currency": "CNY",
        "stock": 12,
        "version": VERSION,
        "as_of": "2026-10-01T12:00:00+00:00",
    }
    assert output.metadata["as_of"] == "2026-10-01T12:00:00+00:00"
    assert output.receipt is not None
    assert output.receipt.evidence_refs == (f"product:{SKU}@{VERSION}#row:1",)


async def test_get_price_stock_reads_the_live_ledger_not_the_catalogue(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=(f"product:{SKU}",))
    store.seed(
        tenant="shop-a",
        sku=SKU,
        name="Synthetic notebook A5 80 pages",
        price_cents=1399,
        stock=2,
        version=VERSION + 1,
    )
    tool = GetPriceStockTool(store, products, clock=lambda: datetime(2026, 10, 2, tzinfo=UTC))
    output = await tool.execute(PriceStockInput(sku=SKU), tool_context(owner))
    payload = json.loads(output.content)
    assert (payload["price_cents"], payload["stock"], payload["version"]) == (1399, 2, VERSION + 1)
    assert output.receipt is not None
    assert output.receipt.evidence_refs == (f"product:{SKU}@{VERSION + 1}#row:1",)


async def test_get_price_stock_refuses_a_naive_clock(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=(f"product:{SKU}",))
    tool = GetPriceStockTool(store, products, clock=lambda: datetime(2026, 10, 1, 12, 0))
    with pytest.raises(ValueError, match="aware"):
        await tool.execute(PriceStockInput(sku=SKU), tool_context(owner))


async def test_get_price_stock_refuses_a_sku_outside_the_scope(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    owner = context(resources=("product:SYN-NB-002",))
    tool = GetPriceStockTool(store, products)
    with pytest.raises(PermissionError, match="outside scope"):
        await tool.execute(PriceStockInput(sku=SKU), tool_context(owner))


async def test_get_price_stock_refuses_a_row_the_catalogue_cannot_price(tmp_path: Path) -> None:
    store, products = seeded(tmp_path)
    store.seed(tenant="shop-a", sku="SYN-XX-999", name="unnamed", price_cents=1, stock=1)
    owner = context(resources=("product:SYN-XX-999",))
    tool = GetPriceStockTool(store, products)
    with pytest.raises(ValueError, match="currency"):
        await tool.execute(PriceStockInput(sku="SYN-XX-999"), tool_context(owner))


def test_search_products_cannot_be_used_without_a_scoped_product(tmp_path: Path) -> None:
    store, _ = seeded(tmp_path)
    spec = SearchProductsTool(store).spec
    decision = CommercePolicy(store).evaluate(
        AuthorizationRequest(
            context=context(resources=()),
            spec=spec,
            call=ToolCall(id="call-1", name="search_products", arguments={"query": "notebook"}),
        )
    )
    assert decision.type is PolicyDecisionType.DENY


def test_policy_denies_every_unregistered_capability(tmp_path: Path) -> None:
    store, _ = seeded(tmp_path)
    spec = ToolSpec(
        name="delete_catalogue",
        description="Not a registered capability.",
        input_schema={"type": "object"},
    )
    decision = CommercePolicy(store).evaluate(
        AuthorizationRequest(
            context=context(resources=(f"product:{SKU}",)),
            spec=spec,
            call=ToolCall(id="call-1", name="delete_catalogue", arguments={"sku": SKU}),
        )
    )
    assert decision.type is PolicyDecisionType.DENY
    assert decision.reason == "unregistered business capability"

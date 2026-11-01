"""Strict synthetic catalogue loading, idempotent seeding and scoped search.

``CommerceStore.products`` is the live ledger: tenant, sku, name, price, stock and
version. The descriptive fields of the synthetic catalogue (category, attributes,
currency and source provenance) are deliberately *not* columns of that table, so this
module keeps the validated catalogue rows with the host and treats the ledger as the
only authority for price, stock and version. A catalogue row the ledger does not hold
is never served: a citation must point at a row that exists now.

Everything here is deterministic. There is no clock, no network and no model, so the
same catalogue, caller scope and query always produce the same result tuple.

Seeding is version-guarded rather than an unconditional upsert: a row whose version is
older than the stored one is refused (``CatalogError``) instead of overwriting newer
business truth, and a re-seed of unchanged content writes nothing at all.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, ValidationError, model_validator

from forgeharness.sdk import FrozenModel, RunContext, SourceAnchor

if TYPE_CHECKING:  # pragma: no cover - import cycle avoided at runtime, types only
    from forgeharness.applications.commerce.application import CommerceStore

# The tool boundary bounds a search query at 200 characters and a page at 20 rows;
# the function-level ceiling is wider so a host can page a larger result itself.
MAX_QUERY_CHARS = 200
MAX_SEARCH_LIMIT = 100
# ``SourceRef.locator`` is bounded at 200 characters, and an anchor locator may not
# contain the anchor delimiters, so a catalogue row that violates this cannot be
# rendered as a governed reference at all.
MAX_LOCATOR_CHARS = 200


class CatalogError(ValueError):
    """The catalogue document, or a seeding request, is not valid business input."""


class CatalogProduct(FrozenModel):
    """One validated catalogue row with its declared provenance.

    Extra keys are refused, so a fixture cannot smuggle a field past this contract.
    """

    tenant_id: str = Field(min_length=1, max_length=128)
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    price_cents: int = Field(gt=0)
    currency: str = Field(min_length=1, max_length=16)
    stock: int = Field(ge=0)
    version: int = Field(ge=1)
    attributes: dict[str, str] = Field(min_length=1)
    source: SourceAnchor

    @model_validator(mode="after")
    def validate_provenance(self) -> CatalogProduct:
        """Refuse provenance that cannot render as one governed anchor."""
        locator = self.source.locator
        if len(locator) > MAX_LOCATOR_CHARS or "@" in locator or "#" in locator:
            raise ValueError("catalogue source locator must render as one anchor locator")
        for key, value in self.attributes.items():
            if not key.strip() or not value.strip():
                raise ValueError("catalogue attribute names and values must not be blank")
        return self


def _invalid(index: int, error: ValidationError) -> CatalogError:
    """One compact, non-echoing message for a rejected catalogue row."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "<row>"
    return CatalogError(f"catalogue product {index} is invalid at {where}: {first['msg']}")


def load_catalog(path: Path) -> tuple[CatalogProduct, ...]:
    """Parse one catalogue document, refusing anything outside the fixture schema.

    Document order is preserved. ``(tenant_id, sku)`` must be unique, every product
    must belong to a declared tenant when the document lists tenants, and every field
    is validated by ``CatalogProduct`` (positive price, non-negative stock, version at
    least 1, non-empty currency).
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise CatalogError(f"catalogue is unreadable: {path}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise CatalogError("catalogue is not valid JSON") from error
    if not isinstance(document, dict):
        raise CatalogError("catalogue document must be a JSON object")
    if document.get("schema_version") != 1:
        raise CatalogError("unsupported catalogue schema")
    rows = document.get("products")
    if not isinstance(rows, list) or not rows:
        raise CatalogError("catalogue must hold a non-empty product list")
    declared: tuple[str, ...] | None = None
    tenants = document.get("tenants")
    if tenants is not None:
        if not isinstance(tenants, list) or not all(
            isinstance(tenant, str) and tenant for tenant in tenants
        ):
            raise CatalogError("catalogue tenants must be non-empty strings")
        declared = tuple(tenants)
    products: list[CatalogProduct] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CatalogError(f"catalogue product {index} must be an object")
        try:
            product = CatalogProduct.model_validate(row)
        except ValidationError as error:
            raise _invalid(index, error) from error
        identity = (product.tenant_id, product.sku)
        if identity in seen:
            raise CatalogError(f"duplicate catalogue product {identity}")
        seen.add(identity)
        if declared is not None and product.tenant_id not in declared:
            raise CatalogError(f"catalogue product {identity} names an undeclared tenant")
        products.append(product)
    return tuple(products)


def catalog_product(
    products: Iterable[CatalogProduct], tenant_id: str, sku: str
) -> CatalogProduct | None:
    """The catalogue row for one tenant and sku, or ``None`` when it declares none."""
    for product in products:
        if product.tenant_id == tenant_id and product.sku == sku:
            return product
    return None


def _validated(products: Iterable[CatalogProduct]) -> tuple[CatalogProduct, ...]:
    """Re-validate a seeding batch, refuse duplicate identities and sort it."""
    rows: list[CatalogProduct] = []
    seen: set[tuple[str, str]] = set()
    for item in products:
        product = CatalogProduct.model_validate(item)
        identity = (product.tenant_id, product.sku)
        if identity in seen:
            raise CatalogError(f"duplicate catalogue product {identity}")
        seen.add(identity)
        rows.append(product)
    rows.sort(key=lambda product: (product.tenant_id, product.sku))
    return tuple(rows)


def seed_catalog(store: CommerceStore, products: Iterable[CatalogProduct]) -> int:
    """Seed catalogue rows into the live product ledger; returns rows applied.

    The whole batch runs in one ``BEGIN IMMEDIATE`` transaction, so a refused batch
    leaves the ledger untouched. For each row:

    * absent from the ledger: inserted;
    * newer version than stored: applied (name, price, stock and version together);
    * exactly the stored version with identical content: a no-op, so re-seeding the
      same fixture writes nothing and returns ``0``;
    * older version than stored: refused with ``CatalogError``.

    The ambiguous case — the stored version with *different* content — is also refused,
    because a version that names two different rows cannot be trusted to order updates.
    """
    rows = _validated(products)
    if not rows:
        return 0
    applied = 0
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for product in rows:
            row = connection.execute(
                "SELECT name, price_cents, stock, version FROM products WHERE tenant=? AND sku=?",
                (product.tenant_id, product.sku),
            ).fetchone()
            incoming = (product.name, product.price_cents, product.stock, product.version)
            if row is None:
                connection.execute(
                    "INSERT INTO products(tenant, sku, name, price_cents, stock, version) "
                    "VALUES(?,?,?,?,?,?)",
                    (product.tenant_id, product.sku, *incoming),
                )
                applied += 1
                continue
            stored = (
                str(row["name"]),
                int(row["price_cents"]),
                int(row["stock"]),
                int(row["version"]),
            )
            where = f"{product.tenant_id}/{product.sku}"
            if product.version < stored[3]:
                raise CatalogError(f"refusing a stale catalogue version for {where}")
            if product.version == stored[3]:
                if incoming != stored:
                    raise CatalogError(
                        f"catalogue version {product.version} for {where} names different content"
                    )
                continue
            connection.execute(
                "UPDATE products SET name=?, price_cents=?, stock=?, version=? "
                "WHERE tenant=? AND sku=?",
                (*incoming, product.tenant_id, product.sku),
            )
            applied += 1
    return applied


def _searchable(product: CatalogProduct) -> str:
    """Case-folded text a query term may match: name, category and attribute pairs."""
    parts = [product.name, product.category]
    for key in sorted(product.attributes):
        parts.extend((key, product.attributes[key]))
    return "\n".join(parts).casefold()


def search_catalog(
    products: Iterable[CatalogProduct], context: RunContext, query: str, *, limit: int = 5
) -> tuple[CatalogProduct, ...]:
    """Search the catalogue for rows the caller's tenant and scope actually cover.

    Tenant and ``scope.resources`` are applied as filters *before* any matching, so a
    row of another tenant, or a row whose ``product:{sku}`` resource is not in the
    caller's scope, can never appear in a result however it is queried. The sku itself
    is not a search field: it is the exact identity a scoped lookup already uses.

    Matching is case-insensitive and conjunctive over whitespace-separated query
    terms, each of which must occur in the row's name, category, attribute names or
    attribute values. Hits are ordered by name and then by sku, which is what makes
    two products sharing one name (the fixture contains such a pair) deterministic.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"catalogue search limit must be between 1 and {MAX_SEARCH_LIMIT}")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("catalogue search query must not be empty")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError("catalogue search query exceeds the maximum length")
    # ``query.strip()`` is non-empty, so at least one term exists.
    terms = tuple(dict.fromkeys(query.casefold().split()))
    scoped = {
        resource.removeprefix("product:")
        for resource in context.scope.resources
        if resource.startswith("product:")
    }
    if not scoped:
        return ()
    tenant = context.scope.tenant_id
    candidates = [
        (product, _searchable(product))
        for product in products
        if product.tenant_id == tenant and product.sku in scoped
    ]
    matches = [product for product, text in candidates if all(term in text for term in terms)]
    matches.sort(key=lambda product: (product.name.casefold(), product.sku))
    return tuple(matches[:limit])


__all__ = [
    "MAX_LOCATOR_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_SEARCH_LIMIT",
    "CatalogError",
    "CatalogProduct",
    "catalog_product",
    "load_catalog",
    "search_catalog",
    "seed_catalog",
]

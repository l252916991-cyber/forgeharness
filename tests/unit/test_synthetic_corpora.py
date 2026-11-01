"""Executable contract for the synthetic legal (S3) and commerce (S4) corpora.

Everything under ``data/synthetic`` is invented test data: no real party, matter,
company, product, brand or contact detail appears in it. This module is the
fixture contract. It fails loudly on a missing file, a malformed document, a
citation that cannot be checked against the page it names, or an accidental
real-world identifier.

``test_created_files_contain_no_real_world_identifiers`` scans this module too,
so the needles it looks for are assembled from fragments at import time instead
of being written out literally.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "data" / "synthetic"
LEGAL_ROOT = FIXTURE_ROOT / "legal"
COMMERCE_ROOT = FIXTURE_ROOT / "commerce"
FAQ_ROOT = COMMERCE_ROOT / "faq"
THIS_FILE = Path(__file__).resolve()

ALPHA = "case-alpha"
BETA = "case-beta"
CASE_TENANTS = {ALPHA: "firm-alpha", BETA: "firm-beta"}
CASE_PAGES = {
    ALPHA: [f"page-{number:02d}.txt" for number in range(1, 7)],
    BETA: ["page-01.txt", "page-02.txt"],
}
PAGE_WORDS = (120, 300)

QUESTION_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
PAGE_LOCATOR = re.compile(r"^page:[1-9][0-9]*$")
DATE = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}")
NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
QUESTION_KEYS = {
    "id",
    "question",
    "kind",
    "expected_case",
    "expected_page",
    "expected_quote",
    "notes",
}
QUESTION_KINDS = {
    "answerable": 5,
    "no_evidence": 2,
    "conflicting_source": 1,
    "cross_case": 1,
    "opinion_only": 1,
}
QUESTION_IDS = {
    "alpha-payment-date",
    "alpha-delivery-deadline",
    "alpha-delivery-date",
    "alpha-invoice-total",
    "alpha-contract-value",
    "alpha-bank-account",
    "alpha-handler-email",
    "alpha-arrival-conflict",
    "alpha-cross-case-refund",
    "alpha-memo-recommendation",
}

CASE_KEYS = {"schema_version", "case_id", "tenant_id", "title", "resources", "pages"}
PAGE_KEYS = {"locator", "file", "summary"}

CATALOG_KEYS = {"schema_version", "generated_for", "tenants", "products"}
PRODUCT_KEYS = {
    "tenant_id",
    "sku",
    "name",
    "category",
    "price_cents",
    "currency",
    "stock",
    "version",
    "attributes",
    "source",
}
SOURCE_KEYS = {"source", "version", "locator"}
COMPARISON_PAIRS = (
    ("SYN-NB-001", "SYN-NB-002"),
    ("SYN-BT-201", "SYN-BT-202"),
    ("SYN-TW-301", "SYN-TW-302"),
    ("SYN-LM-101", "SYN-LM-102"),
)
SHOP_A = "shop-a"
SHOP_B = "shop-b"

FAQ_KEYS = {
    "schema_version",
    "id",
    "tenant_id",
    "question",
    "answer",
    "category",
    "sources",
}
REQUIRED_FAQ_CATEGORIES = {"shipping", "returns", "payment", "warranty", "care"}
# Answers that depend on policy knowledge rather than on any catalogue row.
KNOWLEDGE_ONLY_ANSWERS = {
    "faq-03": "remote-area handling surcharge of 6.00 CNY per parcel",
    "faq-07": "issues a prepaid label",
    "faq-13": "a photo of the fault",
    "faq-15": "at 30 degrees",
}

# Search needles for the identifier scan. The two URL schemes and the brand
# names are split here so that this module survives its own scan.
URL_NEEDLES = ("ht" + "tp://", "ht" + "tps://")
BRAND_NEEDLES = ("Lex" + "Vault", "Ali" + "baba", "Tao" + "bao", "Ama" + "zon")
LONG_DIGIT_RUN = re.compile(r"[0-9]{16,}")
ADDRESS_LIKE = re.compile(
    r"[A-Za-z0-9._%+\-]+" + "@" + r"[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
)

METADATA_NAMES = {".DS_Store"}


def load_json(path: Path) -> Any:
    """Parse one fixture document, reporting the file when it is unreadable."""
    label = path.relative_to(PROJECT_ROOT).as_posix()
    assert path.is_file(), f"missing fixture file: {label}"
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise AssertionError(f"{label} is not valid JSON: {error}") from error


def fixture_files() -> list[Path]:
    """Every authored file under data/synthetic, ignoring OS metadata files."""
    return sorted(
        path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path.name not in METADATA_NAMES
    )


def case_document(case_id: str) -> dict[str, Any]:
    return load_json(LEGAL_ROOT / case_id / "case.json")


def page_path(case_id: str, file_name: str) -> Path:
    return LEGAL_ROOT / case_id / file_name


def page_text(case_id: str, file_name: str) -> str:
    path = page_path(case_id, file_name)
    assert path.is_file(), f"missing page file: {path.relative_to(PROJECT_ROOT).as_posix()}"
    return path.read_text(encoding="utf-8")


def page_name(case_id: str, locator: str) -> str:
    assert isinstance(locator, str) and PAGE_LOCATOR.match(locator), (
        f"bad page locator: {locator!r}"
    )
    number = int(locator.split(":")[1])
    names = CASE_PAGES[case_id]
    assert number <= len(names), f"{case_id} has no {locator}; it has {len(names)} pages"
    return names[number - 1]


def questions() -> list[dict[str, Any]]:
    document = load_json(LEGAL_ROOT / ALPHA / "questions.json")
    assert isinstance(document, dict), "questions.json must be a JSON object"
    return document["questions"]


def question_of_kind(kind: str) -> dict[str, Any]:
    matches = [question for question in questions() if question["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind!r} question, found {len(matches)}"
    return matches[0]


def legal_evidence_files() -> list[Path]:
    """Every legal file that carries evidence: pages and case manifests."""
    paths: list[Path] = []
    for case_id, page_names in CASE_PAGES.items():
        paths.append(LEGAL_ROOT / case_id / "case.json")
        paths.extend(LEGAL_ROOT / case_id / name for name in page_names)
    return paths


def legal_evidence_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in legal_evidence_files())


def catalog() -> dict[str, Any]:
    return load_json(COMMERCE_ROOT / "catalog.json")


def products() -> list[dict[str, Any]]:
    return catalog()["products"]


def products_of(tenant_id: str) -> list[dict[str, Any]]:
    return [product for product in products() if product["tenant_id"] == tenant_id]


def faq_documents() -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(FAQ_ROOT.glob("*.json")):
        documents[path.stem] = load_json(path)
    return documents


def test_fixture_file_set_is_exactly_the_authored_inventory() -> None:
    expected = {
        f"legal/{ALPHA}/case.json",
        f"legal/{ALPHA}/questions.json",
        *(f"legal/{ALPHA}/{name}" for name in CASE_PAGES[ALPHA]),
        f"legal/{BETA}/case.json",
        *(f"legal/{BETA}/{name}" for name in CASE_PAGES[BETA]),
        "commerce/catalog.json",
        *(f"commerce/faq/faq-{number:02d}.json" for number in range(1, 25)),
    }
    actual = {path.relative_to(FIXTURE_ROOT).as_posix() for path in fixture_files()}
    assert len(expected) == 36, "the fixture inventory must hold 36 files"
    assert sorted(expected - actual) == [], f"missing fixture files: {sorted(expected - actual)}"
    assert sorted(actual - expected) == [], f"unexpected fixture files: {sorted(actual - expected)}"
    assert THIS_FILE.name == "test_synthetic_corpora.py", THIS_FILE.name
    assert THIS_FILE.is_file(), f"missing fixture test module: {THIS_FILE}"


def test_every_fixture_json_document_parses() -> None:
    paths = sorted(FIXTURE_ROOT.rglob("*.json"))
    assert len(paths) == 28, f"expected 28 JSON fixtures, found {len(paths)}"
    for path in paths:
        document = load_json(path)
        label = path.relative_to(PROJECT_ROOT).as_posix()
        assert isinstance(document, dict), f"{label} must hold a JSON object"
        assert document, f"{label} must not be an empty object"


def test_legal_case_manifests_validate_field_by_field() -> None:
    tenants = set()
    for case_id, expected_pages in CASE_PAGES.items():
        where = f"legal/{case_id}/case.json"
        document = case_document(case_id)
        assert set(document) == CASE_KEYS, f"{where}: keys are {sorted(document)}"
        assert document["schema_version"] == 1, f"{where}: schema_version must be 1"
        assert document["case_id"] == case_id, f"{where}: case_id must be {case_id!r}"
        assert document["tenant_id"] == CASE_TENANTS[case_id], f"{where}: wrong tenant_id"
        tenants.add(document["tenant_id"])
        title = document["title"]
        assert isinstance(title, str) and "fixture" in title.lower(), f"{where}: title {title!r}"
        assert document["resources"] == [f"case:{case_id}", f"source:{case_id}"], (
            f"{where}: resources must scope exactly this case"
        )
        pages = document["pages"]
        assert isinstance(pages, list), f"{where}: pages must be a list"
        assert len(pages) == len(expected_pages), (
            f"{where}: expected {len(expected_pages)} pages, found {len(pages)}"
        )
        for index, (page, file_name) in enumerate(zip(pages, expected_pages, strict=True), 1):
            assert isinstance(page, dict), f"{where}: page {index} must be an object"
            assert set(page) == PAGE_KEYS, f"{where}: page {index} keys are {sorted(page)}"
            assert page["locator"] == f"page:{index}", f"{where}: page {index} locator"
            assert page["file"] == file_name, f"{where}: page {index} file must be {file_name!r}"
            summary = page["summary"]
            assert isinstance(summary, str) and summary.strip(), f"{where}: page {index} summary"
            assert page_path(case_id, file_name).is_file(), f"{where}: {file_name} is missing"
    assert tenants == {"firm-alpha", "firm-beta"}, "the two cases must belong to different tenants"


def test_legal_pages_are_plain_synthetic_text() -> None:
    for case_id, page_names in CASE_PAGES.items():
        for file_name in page_names:
            text = page_text(case_id, file_name)
            words = len(text.split())
            assert PAGE_WORDS[0] <= words <= PAGE_WORDS[1], (
                f"legal/{case_id}/{file_name} has {words} words, outside {PAGE_WORDS}"
            )
            assert text.splitlines()[0].strip(), (
                f"legal/{case_id}/{file_name} starts with a blank line"
            )
    for case_id in CASE_PAGES:
        first_line = page_text(case_id, "page-01.txt").splitlines()[0]
        assert "synthetic fixture" in first_line.lower(), (
            f"legal/{case_id}/page-01.txt must declare itself a synthetic fixture on line one"
        )


def test_questions_document_shape_and_kind_counts() -> None:
    document = load_json(LEGAL_ROOT / ALPHA / "questions.json")
    assert set(document) == {"schema_version", "case_id", "questions"}
    assert document["schema_version"] == 1
    assert document["case_id"] == ALPHA
    items = document["questions"]
    assert isinstance(items, list) and len(items) == 10, "questions.json must hold 10 questions"
    kinds = Counter(question["kind"] for question in items)
    assert dict(kinds) == QUESTION_KINDS, f"question kinds are {dict(kinds)}"
    ids = [question["id"] for question in items]
    assert len(set(ids)) == len(ids), f"duplicate question ids: {ids}"
    assert set(ids) == QUESTION_IDS, f"unexpected question ids: {sorted(set(ids) ^ QUESTION_IDS)}"
    for question in items:
        where = f"questions.json[{question['id']}]"
        assert set(question) == QUESTION_KEYS, f"{where}: keys are {sorted(question)}"
        assert QUESTION_ID.match(question["id"]), f"{where}: id does not match the id pattern"
        text = question["question"]
        assert isinstance(text, str) and text.endswith("?"), f"{where}: question text {text!r}"
        assert isinstance(question["notes"], str) and question["notes"].strip(), f"{where}: notes"
        assert question["expected_case"] in CASE_TENANTS, f"{where}: expected_case"
        quote = question["expected_quote"]
        assert isinstance(quote, str) and quote.strip(), f"{where}: expected_quote"
        if question["kind"] == "no_evidence":
            assert question["expected_page"] is None, f"{where}: no page may be claimed"
        else:
            page_name(question["expected_case"], question["expected_page"])


def test_answerable_quotes_are_verbatim_and_appear_on_one_page_only() -> None:
    checked = 0
    for question in questions():
        if question["kind"] != "answerable":
            continue
        checked += 1
        where = question["id"]
        assert question["expected_case"] == ALPHA, f"{where}: answerable facts live in {ALPHA}"
        target = page_name(ALPHA, question["expected_page"])
        quote = question["expected_quote"]
        assert quote in page_text(ALPHA, target), (
            f"{where}: {quote!r} is not verbatim in legal/{ALPHA}/{target}"
        )
        hits = [name for name in CASE_PAGES[ALPHA] if quote in page_text(ALPHA, name)]
        assert hits == [target], (
            f"{where}: {quote!r} also appears in {hits}, so page citation is ambiguous"
        )
    assert checked == 5, f"expected 5 answerable questions, checked {checked}"


def test_no_evidence_quotes_appear_nowhere_in_the_legal_corpus() -> None:
    corpus = legal_evidence_text()
    checked = 0
    for question in questions():
        if question["kind"] != "no_evidence":
            continue
        checked += 1
        quote = question["expected_quote"]
        assert question["expected_page"] is None, f"{question['id']}: no page may be claimed"
        assert quote not in corpus, (
            f"{question['id']}: {quote!r} must not exist anywhere in the legal corpus"
        )
    assert checked == 2, f"expected 2 no_evidence questions, checked {checked}"


def test_cross_case_question_resolves_only_in_the_other_case() -> None:
    question = question_of_kind("cross_case")
    assert question["expected_case"] == BETA, "the cross_case fact must live in the other case"
    target = page_name(BETA, question["expected_page"])
    quote = question["expected_quote"]
    assert quote in page_text(BETA, target), f"{quote!r} is not verbatim in legal/{BETA}/{target}"
    beta_hits = [name for name in CASE_PAGES[BETA] if quote in page_text(BETA, name)]
    assert beta_hits == [target], f"{quote!r} also appears in {beta_hits}"
    alpha_text = "\n".join(page_text(ALPHA, name) for name in CASE_PAGES[ALPHA])
    assert quote not in alpha_text, (
        f"{question['id']}: a search scoped to {ALPHA} must return no evidence for {quote!r}"
    )


def test_conflicting_source_question_points_at_a_real_contradiction() -> None:
    question = question_of_kind("conflicting_source")
    assert question["expected_case"] == ALPHA
    quote = question["expected_quote"]
    target = page_name(ALPHA, question["expected_page"])
    assert quote in page_text(ALPHA, target), f"{quote!r} is not verbatim in {target}"
    quote_hits = [name for name in CASE_PAGES[ALPHA] if quote in page_text(ALPHA, name)]
    assert quote_hits == [target], f"{quote!r} also appears in {quote_hits}"

    delivery = next(
        item
        for item in questions()
        if item["kind"] == "answerable" and item["expected_page"] == "page:3"
    )
    delivery_quote = delivery["expected_quote"]
    assert delivery_quote not in page_text(ALPHA, target), (
        f"{target} must not repeat the delivery note's statement {delivery_quote!r}"
    )
    assert quote not in page_text(ALPHA, page_name(ALPHA, delivery["expected_page"])), (
        "the delivery note must not repeat the witness statement"
    )
    witness_dates = set(DATE.findall(quote))
    note_dates = set(DATE.findall(delivery_quote))
    assert witness_dates and note_dates, "both sources must state a date"
    assert witness_dates.isdisjoint(note_dates), (
        f"the witness statement {witness_dates} and the delivery note {note_dates} must disagree"
    )


def test_opinion_only_question_cites_a_memo_that_states_no_fact() -> None:
    question = question_of_kind("opinion_only")
    assert question["expected_case"] == ALPHA
    assert question["expected_page"] == "page:6", "the memo is page 6"
    text = page_text(ALPHA, "page-06.txt")
    assert question["expected_quote"] in text, (
        f"{question['expected_quote']!r} is not in page-06.txt"
    )
    assert not re.search(r"[0-9]", text), "the memo must state no verifiable figure"
    assert not DATE.search(text), "the memo must state no verifiable date"
    hits = [
        name for name in CASE_PAGES[ALPHA] if question["expected_quote"] in page_text(ALPHA, name)
    ]
    assert hits == ["page-06.txt"], f"{question['expected_quote']!r} also appears in {hits}"


def test_catalog_manifest_validates_field_by_field() -> None:
    document = catalog()
    assert set(document) == CATALOG_KEYS, f"catalog.json keys are {sorted(document)}"
    assert document["schema_version"] == 1
    assert document["generated_for"] == "synthetic consultation MVP (no real store)"
    assert document["tenants"] == [SHOP_A, SHOP_B], "the catalogue must declare both tenants"
    rows = document["products"]
    assert isinstance(rows, list), "products must be a list"
    assert len(rows) == 36, f"expected 36 products, found {len(rows)}"
    for index, product in enumerate(rows):
        assert isinstance(product, dict), f"catalog[{index}]: must be an object"
        where = f"catalog[{product.get('tenant_id')}/{product.get('sku')}]"
        assert set(product) == PRODUCT_KEYS, f"{where}: keys are {sorted(product)}"
        assert product["tenant_id"] in document["tenants"], f"{where}: unknown tenant"
        for field in ("sku", "name", "category"):
            value = product[field]
            assert isinstance(value, str) and value.strip(), f"{where}: {field} is {value!r}"
        price = product["price_cents"]
        assert isinstance(price, int) and not isinstance(price, bool), f"{where}: price_cents type"
        assert price > 0, f"{where}: price_cents must be positive"
        currency = product["currency"]
        assert isinstance(currency, str) and currency == "CNY", f"{where}: currency is {currency!r}"
        stock = product["stock"]
        assert isinstance(stock, int) and not isinstance(stock, bool), f"{where}: stock type"
        assert stock >= 0, f"{where}: stock must not be negative"
        version = product["version"]
        assert isinstance(version, int) and not isinstance(version, bool), f"{where}: version type"
        assert version >= 1, f"{where}: version must be a positive integer"
        attributes = product["attributes"]
        assert isinstance(attributes, dict) and attributes, f"{where}: attributes must be non-empty"
        for key, value in attributes.items():
            assert isinstance(key, str) and key.strip(), f"{where}: attribute key {key!r}"
            assert isinstance(value, str) and value.strip(), f"{where}: attribute {key}={value!r}"
        source = product["source"]
        assert isinstance(source, dict), f"{where}: source must be an object"
        assert set(source) == SOURCE_KEYS, f"{where}: source keys are {sorted(source)}"
        for field in sorted(SOURCE_KEYS):
            value = source[field]
            assert isinstance(value, str) and value.strip(), f"{where}: source.{field} is {value!r}"


def test_catalog_tenant_split_uniqueness_and_sku_collision() -> None:
    rows = products()
    shop_a = products_of(SHOP_A)
    shop_b = products_of(SHOP_B)
    assert len(shop_a) == 30, f"expected 30 {SHOP_A} products, found {len(shop_a)}"
    assert len(shop_b) == 6, f"expected 6 {SHOP_B} products, found {len(shop_b)}"
    identity = [(product["tenant_id"], product["sku"]) for product in rows]
    assert len(set(identity)) == len(rows), "(tenant_id, sku) must be unique across the catalogue"

    index = {(product["tenant_id"], product["sku"]): product for product in rows}
    collision = {product["sku"] for product in shop_a} & {product["sku"] for product in shop_b}
    assert collision, "the isolated tenant must reuse at least one shop-a sku string"
    for sku in sorted(collision):
        first, second = index[(SHOP_A, sku)], index[(SHOP_B, sku)]
        assert first["name"] != second["name"], f"{sku}: the two tenants must not share a name"
        assert first["price_cents"] != second["price_cents"], f"{sku}: prices must differ"
        assert first["stock"] != second["stock"], f"{sku}: stock must differ"
    assert {product["name"] for product in shop_a}.isdisjoint(
        {product["name"] for product in shop_b}
    ), f"{SHOP_B} must list completely different products"
    assert {product["price_cents"] for product in shop_a}.isdisjoint(
        {product["price_cents"] for product in shop_b}
    ), f"{SHOP_B} must list completely different prices"


def test_catalog_comparison_pairs_support_consultation() -> None:
    shop_a = {product["sku"]: product for product in products_of(SHOP_A)}
    compared: set[str] = set()
    for left, right in COMPARISON_PAIRS:
        assert left in shop_a and right in shop_a, f"comparison pair {left}/{right} must exist"
        first, second = shop_a[left], shop_a[right]
        assert first["category"] == second["category"], f"{left}/{right}: same category is required"
        assert first["price_cents"] != second["price_cents"], f"{left}/{right}: prices must differ"
        assert first["attributes"] != second["attributes"], (
            f"{left}/{right}: attributes must differ"
        )
        shared = set(first["attributes"]) & set(second["attributes"])
        assert shared, f"{left}/{right}: a comparable pair must share an attribute key"
        assert any(
            first["attributes"][key] != second["attributes"][key] for key in sorted(shared)
        ), f"{left}/{right}: the shared attribute must hold different values"
        compared.update({left, right})
    assert len(compared) == 8, f"expected 8 products in comparison pairs, found {len(compared)}"

    categories: dict[str, set[int]] = {}
    for product in products_of(SHOP_A):
        categories.setdefault(product["category"], set()).add(product["price_cents"])
    comparable = [category for category, prices in categories.items() if len(prices) > 1]
    assert len(comparable) >= 4, f"only {sorted(comparable)} allow a price comparison"


def test_catalog_stock_disambiguation_locators_and_version_bump() -> None:
    rows = products()
    zero_stock = [product for product in rows if product["stock"] == 0]
    assert len(zero_stock) >= 2, (
        f"expected at least 2 out-of-stock products, found {len(zero_stock)}"
    )
    assert len([p for p in zero_stock if p["tenant_id"] == SHOP_A]) >= 2, "shop-a needs zero stock"

    by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for product in rows:
        by_name.setdefault((product["tenant_id"], product["name"]), []).append(product)
    shared_names = [group for group in by_name.values() if len(group) > 1]
    assert shared_names, "two products must share a name to exercise disambiguation"
    for group in shared_names:
        assert len({product["sku"] for product in group}) == len(group), (
            "shared names need distinct skus"
        )
        assert len({product["version"] for product in group}) == len(group), (
            "shared names need distinct versions"
        )

    for tenant in (SHOP_A, SHOP_B):
        locators = [product["source"]["locator"] for product in products_of(tenant)]
        assert len(set(locators)) == len(locators), f"{tenant}: source.locator must be unique"

    bumpable = [product for product in rows if product["version"] >= 2]
    assert len(bumpable) >= 3, f"at least 3 products must be bumpable, found {len(bumpable)}"
    for product in bumpable:
        bumped = {**product, "version": product["version"] + 1}
        where = f"catalog[{product['tenant_id']}/{product['sku']}]"
        assert bumped["version"] == product["version"] + 1, f"{where}: version bump must increment"
        assert bumped != product, f"{where}: a version bump must be observable"
        assert bumped["sku"] == product["sku"], f"{where}: a version bump must not change identity"
        assert json.dumps(bumped, sort_keys=True) != json.dumps(product, sort_keys=True), (
            f"{where}: the bumped row must serialise differently"
        )


def test_faq_corpus_shape_matches_the_contract() -> None:
    documents = faq_documents()
    assert len(documents) == 24, f"expected 24 FAQ files, found {len(documents)}"
    expected_ids = [f"faq-{number:02d}" for number in range(1, 25)]
    assert sorted(documents) == expected_ids, f"FAQ ids are {sorted(documents)}"
    tenants: Counter[str] = Counter()
    for faq_id in expected_ids:
        document = documents[faq_id]
        where = f"commerce/faq/{faq_id}.json"
        keys = set(document)
        assert keys == FAQ_KEYS or keys == FAQ_KEYS | {"conflict_with"}, (
            f"{where}: keys are {sorted(keys)}"
        )
        assert document["schema_version"] == 1, f"{where}: schema_version must be 1"
        assert document["id"] == faq_id, f"{where}: id must match the file name"
        tenant = document["tenant_id"]
        assert tenant in {SHOP_A, SHOP_B}, f"{where}: unknown tenant {tenant!r}"
        tenants[tenant] += 1
        for field in ("question", "answer", "category"):
            value = document[field]
            assert isinstance(value, str) and value.strip(), f"{where}: {field} is {value!r}"
        sources = document["sources"]
        assert isinstance(sources, list) and sources, f"{where}: at least one source is required"
        for source in sources:
            assert isinstance(source, dict), f"{where}: source must be an object"
            assert set(source) == SOURCE_KEYS, f"{where}: source keys are {sorted(source)}"
            for field in sorted(SOURCE_KEYS):
                value = source[field]
                assert isinstance(value, str) and value.strip(), f"{where}: source.{field}"
    assert tenants == {SHOP_A: 20, SHOP_B: 4}, f"FAQ tenant split is {dict(tenants)}"


def test_faq_categories_and_conflicting_pairs() -> None:
    documents = faq_documents()
    categories = {document["category"] for document in documents.values()}
    assert REQUIRED_FAQ_CATEGORIES <= categories, (
        f"missing FAQ categories: {sorted(REQUIRED_FAQ_CATEGORIES - categories)}"
    )
    shop_a_categories = {
        document["category"] for document in documents.values() if document["tenant_id"] == SHOP_A
    }
    assert REQUIRED_FAQ_CATEGORIES <= shop_a_categories, (
        f"{SHOP_A} is missing categories: {sorted(REQUIRED_FAQ_CATEGORIES - shop_a_categories)}"
    )

    declared = {
        faq_id: document["conflict_with"]
        for faq_id, document in documents.items()
        if "conflict_with" in document
    }
    assert len(declared) >= 4, f"expected at least 4 conflicting FAQs, found {len(declared)}"
    for faq_id, other in declared.items():
        assert other in documents, f"{faq_id}: conflict_with points at unknown {other!r}"
        assert other != faq_id, f"{faq_id}: an FAQ cannot conflict with itself"
        assert "conflict_with" in documents[other], f"{other} must declare the conflict back"
        assert documents[other]["conflict_with"] == faq_id, (
            f"{faq_id}/{other}: the conflict declaration must be symmetric"
        )
        assert documents[other]["tenant_id"] == documents[faq_id]["tenant_id"], (
            f"{faq_id}/{other}: conflicts must stay inside one tenant"
        )
        first = documents[faq_id]["answer"]
        second = documents[other]["answer"]
        assert first != second, f"{faq_id}/{other}: conflicting answers must differ"
        assert NUMBER.findall(first) != NUMBER.findall(second), (
            f"{faq_id}/{other}: the conflict must turn on a concrete detail"
        )


def test_faq_answers_that_require_knowledge_beyond_the_catalog() -> None:
    documents = faq_documents()
    catalog_text = (COMMERCE_ROOT / "catalog.json").read_text(encoding="utf-8")
    assert len(KNOWLEDGE_ONLY_ANSWERS) >= 3, (
        "at least 3 FAQs must need knowledge, not a product row"
    )
    for faq_id, detail in KNOWLEDGE_ONLY_ANSWERS.items():
        assert faq_id in documents, f"{faq_id} must exist"
        assert detail in documents[faq_id]["answer"], f"{faq_id} must state {detail!r}"
        assert detail not in catalog_text, (
            f"{detail!r} must not be derivable from the product catalogue"
        )
        for product in products():
            assert detail not in json.dumps(product, sort_keys=True), (
                f"{faq_id}: {detail!r} leaked into a product row"
            )


def test_created_files_contain_no_real_world_identifiers() -> None:
    scanned = [*fixture_files(), THIS_FILE]
    assert len(scanned) == 37, f"the scan must cover all 37 created files, found {len(scanned)}"
    findings: list[str] = []
    for path in scanned:
        label = path.relative_to(PROJECT_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for needle in URL_NEEDLES + BRAND_NEEDLES:
            if needle in text:
                findings.append(f"{label}: contains {needle!r}")
        for match in LONG_DIGIT_RUN.finditer(text):
            findings.append(f"{label}: contains a {len(match.group(0))}-digit run")
        for match in ADDRESS_LIKE.finditer(text):
            findings.append(f"{label}: contains the address-like token {match.group(0)!r}")
    assert not findings, "synthetic fixtures must not name real-world resources:\n" + "\n".join(
        findings
    )

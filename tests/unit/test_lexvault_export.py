# ruff: noqa: RUF001
# Chinese legal fixtures intentionally use full-width punctuation.
"""Export a LexVault-shaped SQLite case and run four read-only DAG questions."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from forgeharness.applications.legal.lexvault import (
    BundleCaseAdapter,
    BundleCitationVerifier,
    LexVaultDagOrchestrator,
    bundle_from_lexvault_db,
    lexvault_dag_application,
)
from forgeharness.sdk import (
    Actor,
    FinalAction,
    Harness,
    ModelResult,
    ResourceScope,
    RunContext,
    RunStatus,
    ScriptedModel,
)


def make_lexvault_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE cases (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                case_id INTEGER NOT NULL,
                name TEXT NOT NULL
            );
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL,
                page_no INTEGER NOT NULL,
                text TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            );
            """
        )
        connection.execute(
            "INSERT INTO cases(id, title, updated_at) VALUES (?, ?, ?)",
            (1, "示例案件", "2026-10-18T10:00:00+08:00"),
        )
        connection.execute(
            "INSERT INTO cases(id, title, updated_at) VALUES (?, ?, ?)",
            (2, "空案件", "2026-10-18T10:00:00+08:00"),
        )
        connection.execute(
            "INSERT INTO documents(id, case_id, name) VALUES (?, ?, ?)",
            (10, 1, "合同.pdf"),
        )
        connection.execute(
            "INSERT INTO documents(id, case_id, name) VALUES (?, ?, ?)",
            (11, 1, "流水.pdf"),
        )
        connection.executemany(
            "INSERT INTO pages(document_id, page_no, text, summary) VALUES (?, ?, ?, ?)",
            [
                (10, 1, "甲公司向乙公司支付资金500万元，用于设备采购。", "付款"),
                (10, 2, "合同约定设备应在30日内交付。", "约定"),
                (11, 1, "证据清单：付款凭证、合同附件、银行流水。", "证据"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def bundle_for_export(tmp_path: Path):
    db_path = tmp_path / "law_review.db"
    make_lexvault_db(db_path)
    return db_path, bundle_from_lexvault_db(
        db_path=db_path,
        case_id=1,
        tenant_id="tenant-a",
    )


def context(run_id: str) -> RunContext:
    return RunContext(
        application_id="lexvault-dag",
        application_version="1",
        policy_version="1",
        run_id=run_id,
        session_id="session-1",
        actor=Actor(id="lawyer", roles=("use",)),
        scope=ResourceScope(
            tenant_id="tenant-a",
            resources=(
                "case:1",
                "case:1/doc:10/page:1",
                "case:1/doc:10/page:2",
                "case:1/doc:11/page:1",
            ),
        ),
        correlation_id="correlation-1",
    )


def test_export_preserves_document_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "law_review.db"
    make_lexvault_db(db_path)

    bundle = bundle_from_lexvault_db(db_path=db_path, case_id=1, tenant_id="tenant-a")

    assert bundle.synthetic is False
    assert bundle.case_id == "1"
    assert [page.page_no for page in bundle.pages] == [1, 2, 3]
    assert [page.locator for page in bundle.pages] == [
        "doc:10/page:1",
        "doc:10/page:2",
        "doc:11/page:1",
    ]
    assert bundle.pages[0].document_name == "合同.pdf"
    assert bundle.pages[0].document_page_no == 1


def test_missing_or_empty_case_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "law_review.db"
    make_lexvault_db(db_path)

    with pytest.raises(ValueError, match="does not exist"):
        bundle_from_lexvault_db(db_path=db_path, case_id=99, tenant_id="tenant-a")
    with pytest.raises(ValueError, match="no non-empty pages"):
        bundle_from_lexvault_db(db_path=db_path, case_id=2, tenant_id="tenant-a")


async def test_four_read_only_questions_on_exported_bundle(tmp_path: Path) -> None:
    db_path, bundle = bundle_for_export(tmp_path)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    adapter = BundleCaseAdapter(bundle)
    verifier = BundleCitationVerifier(bundle)
    app = lexvault_dag_application(
        model_factory=lambda: ScriptedModel(
            [ModelResult(action=FinalAction(content="甲公司支付500万元[page:1]。"))]
        ),
        verifier=verifier,
    )
    harness = Harness(
        app,
        state_dir=tmp_path / "state",
        orchestrator=LexVaultDagOrchestrator(adapter, case_id="1"),
    )

    questions = (
        "梳理资金流向",
        "核对合同金额",
        "是否存在矛盾",
        "有哪些证据",
    )
    results = [
        await harness.run(context=context(f"run-{index}"), task=question)
        for index, question in enumerate(questions, start=1)
    ]

    assert all(result.status is RunStatus.SUCCEEDED for result in results)
    assert all(result.usage.steps == 1 for result in results)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_export_preserves_raw_page_text(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE cases (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                case_id INTEGER NOT NULL,
                name TEXT NOT NULL
            );
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL,
                page_no INTEGER NOT NULL,
                text TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            );
            """
        )
        connection.execute("INSERT INTO cases VALUES (1, 'raw', '2026-10-20')")
        connection.execute("INSERT INTO documents VALUES (7, 1, 'raw.pdf')")
        connection.executemany(
            "INSERT INTO pages(document_id, page_no, text, summary) VALUES (?, ?, ?, ?)",
            [
                (7, 1, "  首尾空白保留  ", ""),
                (7, 2, "   ", ""),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    bundle = bundle_from_lexvault_db(db_path=db_path, case_id=1, tenant_id="t")

    assert len(bundle.pages) == 1, "whitespace-only pages are skipped, raw pages are not stripped"
    assert bundle.pages[0].text == "  首尾空白保留  "
    assert bundle.pages[0].text_sha256 == hashlib.sha256("  首尾空白保留  ".encode()).hexdigest()


def test_version_changes_when_page_text_changes(tmp_path: Path) -> None:
    db_path = tmp_path / "law_review.db"
    make_lexvault_db(db_path)
    first = bundle_from_lexvault_db(db_path=db_path, case_id=1, tenant_id="tenant-a")

    # Edit one page WITHOUT touching cases.updated_at: the version must still move,
    # because it is bound to page digests rather than only to the case timestamp.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE pages SET text = '修改后的资金记录' WHERE document_id = 10 AND page_no = 1"
        )
        connection.commit()
    finally:
        connection.close()

    second = bundle_from_lexvault_db(db_path=db_path, case_id=1, tenant_id="tenant-a")

    assert first.version != second.version
    assert first.snapshot_digest != second.snapshot_digest
    assert second.pages[0].text == "修改后的资金记录"

"""Read-only export from a LexVault-compatible SQLite database to a CaseBundle.

The exporter is deliberately narrow: it reads ``cases``, ``documents`` and ``pages``
using a read-only SQLite URI, assigns a bundle-global citation number to every
non-empty page, and preserves the document-local locator. It never writes to the
source database and does not import LexVault code.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .bundle import BundlePage, CaseBundle, text_digest


def bundle_from_lexvault_db(
    *,
    db_path: Path,
    case_id: int,
    tenant_id: str,
    version: str | None = None,
) -> CaseBundle:
    """Export one case from a LexVault-shaped database.

    The caller maps its authenticated tenant/case scope to ``tenant_id``; this
    function performs no authorization beyond the explicit case id.

    The case row and its pages are read inside one explicit read transaction, so
    a concurrent writer cannot interleave a half-updated case into the snapshot.
    Page text is stored verbatim (``strip`` is only used to skip blank pages),
    because citation offsets and digests are computed against the raw text.
    """

    path = db_path.expanduser().resolve()
    if not path.is_file():
        raise ValueError("LexVault database does not exist")
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.isolation_level = None  # explicit transaction control
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            case = connection.execute(
                "SELECT id, title, updated_at FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
            if case is None:
                raise ValueError("LexVault case does not exist")
            rows = connection.execute(
                """
                SELECT d.id AS document_id,
                       d.name AS document_name,
                       p.page_no AS document_page_no,
                       p.text AS text,
                       p.summary AS summary
                FROM pages p
                JOIN documents d ON d.id = p.document_id
                WHERE d.case_id = ?
                ORDER BY d.id, p.page_no
                """,
                (case_id,),
            ).fetchall()
        finally:
            connection.execute("COMMIT")
    finally:
        connection.close()

    pages: list[BundlePage] = []
    for row in rows:
        raw = str(row["text"] or "")
        if not raw.strip():
            continue
        pages.append(
            BundlePage(
                page_no=len(pages) + 1,
                document_id=int(row["document_id"]),
                document_name=str(row["document_name"])[:300],
                document_page_no=int(row["document_page_no"]),
                text=raw,
                text_sha256=text_digest(raw),
                summary=str(row["summary"] or "")[:300],
            )
        )
    if not pages:
        raise ValueError("LexVault case has no non-empty pages")
    if len(pages) > 100:
        raise ValueError("LexVault case exceeds the 100-page bundle limit")
    if version is None:
        # Bind the version to page content, not just to cases.updated_at: an edit
        # that forgets to bump updated_at must still produce a new bundle version.
        pages_digest = hashlib.sha256(
            "|".join(page.text_sha256 for page in pages).encode("utf-8")
        ).hexdigest()[:16]
        version = f"{case['id']}:{case['updated_at']}:{pages_digest}"
    return CaseBundle(
        tenant_id=tenant_id,
        case_id=str(case["id"]),
        title=str(case["title"]),
        version=version,
        pages=tuple(pages),
        synthetic=False,
    )


__all__ = ["bundle_from_lexvault_db"]

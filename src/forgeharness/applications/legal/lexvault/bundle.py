"""Read-only LexVault case bundle: pages plus case metadata, no database."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from forgeharness.sdk import FrozenModel

MAX_PAGE_CHARS = 20_000
MAX_PAGES = 100
MAX_BUNDLE_CHARS = 200_000


def text_digest(text: str) -> str:
    """Canonical UTF-8 sha256 of one page's text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class BundlePage(FrozenModel):
    """One page of a read-only case bundle. ``text`` is untrusted reference data.

    ``page_no`` is the bundle-global citation number used by ``[page:N]``.
    ``document_id``/``document_page_no`` preserve LexVault's document-local page
    identity when the bundle was exported from its database. ``text_sha256`` is
    derived automatically when absent and must match ``text`` when supplied, so a
    serialized snapshot cannot be re-labelled onto different content.
    """

    page_no: int = Field(ge=1)
    document_id: int | None = Field(default=None, ge=1)
    document_name: str = Field(default="", max_length=300)
    document_page_no: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=MAX_PAGE_CHARS)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(default="", max_length=300)

    @model_validator(mode="before")
    @classmethod
    def _derive_digest(cls, data: Any) -> Any:
        if isinstance(data, dict):
            text = data.get("text")
            if isinstance(text, str) and not data.get("text_sha256"):
                data = {**data, "text_sha256": text_digest(text)}
        return data

    @model_validator(mode="after")
    def _validate_integrity(self) -> BundlePage:
        if (self.document_id is None) != (self.document_page_no is None):
            raise ValueError("document_id and document_page_no must be supplied together")
        if text_digest(self.text) != self.text_sha256:
            raise ValueError("page text does not match its text_sha256 digest")
        return self

    @property
    def locator(self) -> str:
        if self.document_id is not None and self.document_page_no is not None:
            return f"doc:{self.document_id}/page:{self.document_page_no}"
        return f"page:{self.page_no}"


class CaseBundle(FrozenModel):
    """The smallest host boundary needed to run a legal DAG without a database."""

    schema_version: Literal[1] = 1
    tenant_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    version: str = Field(default="1", min_length=1, max_length=100)
    pages: tuple[BundlePage, ...] = Field(min_length=1, max_length=MAX_PAGES)
    synthetic: bool = True

    @model_validator(mode="after")
    def _validate_pages(self) -> CaseBundle:
        numbers = [page.page_no for page in self.pages]
        if len(numbers) != len(set(numbers)):
            raise ValueError("case bundle contains duplicate page numbers")
        locators = [page.locator for page in self.pages]
        if len(locators) != len(set(locators)):
            raise ValueError("case bundle contains duplicate page locators")
        if sum(len(page.text) for page in self.pages) > MAX_BUNDLE_CHARS:
            raise ValueError("case bundle text exceeds the configured limit")
        return self

    @property
    def page_locators(self) -> tuple[str, ...]:
        return tuple(page.locator for page in self.pages)

    def page_by_no(self, page_no: int) -> BundlePage | None:
        return next((page for page in self.pages if page.page_no == page_no), None)

    def page_by_locator(self, locator: str) -> BundlePage | None:
        return next((page for page in self.pages if page.locator == locator), None)

    @property
    def snapshot_digest(self) -> str:
        """Content-addressed identity of the whole snapshot.

        Any change to page text, order, locators or version changes this digest,
        which makes a stale bundle detectable when it is re-exported or reloaded.
        """

        payload = json.dumps(
            {
                "tenant_id": self.tenant_id,
                "case_id": self.case_id,
                "version": self.version,
                "pages": [
                    {
                        "locator": page.locator,
                        "page_no": page.page_no,
                        "text_sha256": page.text_sha256,
                    }
                    for page in self.pages
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_BUNDLE_CHARS",
    "MAX_PAGES",
    "MAX_PAGE_CHARS",
    "BundlePage",
    "CaseBundle",
    "text_digest",
]

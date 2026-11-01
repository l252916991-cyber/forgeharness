"""Read-only CaseAdapter over a CaseBundle. No database and no publication."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from forgeharness.applications.legal.adapter import (
    CaseHit,
    CasePage,
    CaseUnavailable,
    CaseView,
)
from forgeharness.sdk import RecordDraft, RunContext, SourceAnchor

from .bundle import BundlePage, CaseBundle

MAX_QUERY_CHARS = 500
MAX_HIT_CHARS = 180


class BundleCaseAdapter:
    """Authorize and read one preloaded case bundle.

    This is a host-boundary fixture for the framework's legal DAG. It is not a
    LexVault production adapter: it does not read ``law_review.db``, does not
    implement hybrid retrieval, and publishes nothing.
    """

    def __init__(self, bundle: CaseBundle) -> None:
        self.bundle = bundle

    def _authorized(self, context: RunContext, case_id: str) -> bool:
        return (
            case_id == self.bundle.case_id
            and context.scope.tenant_id == self.bundle.tenant_id
            and f"case:{case_id}" in context.scope.resources
        )

    def cases(self, context: RunContext) -> tuple[str, ...]:
        return (self.bundle.case_id,) if self._authorized(context, self.bundle.case_id) else ()

    def case(self, context: RunContext, case_id: str) -> CaseView:
        if not self._authorized(context, case_id):
            raise CaseUnavailable("case unavailable for this binding")
        return CaseView(
            case_id=self.bundle.case_id,
            tenant_id=self.bundle.tenant_id,
            title=self.bundle.title,
            page_locators=self.bundle.page_locators,
            synthetic=self.bundle.synthetic,
        )

    def page(self, context: RunContext, case_id: str, locator: str) -> CasePage:
        view = self.case(context, case_id)
        if (
            locator not in view.page_locators
            or f"case:{case_id}/{locator}" not in context.scope.resources
        ):
            raise CaseUnavailable("page unavailable for this case")
        page = self._page(locator)
        return CasePage(locator=page.locator, summary=page.summary, content=page.text)

    def search(
        self, context: RunContext, case_id: str, query: str, *, limit: int = 5
    ) -> tuple[CaseHit, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be a bounded non-empty string")
        view = self.case(context, case_id)
        terms = [term for term in re.split(r"\W+", query.lower()) if len(term) >= 2]
        if not terms:
            raise ValueError("query has no searchable terms")
        hits: list[CaseHit] = []
        for locator in view.page_locators:
            if f"case:{case_id}/{locator}" not in context.scope.resources:
                continue
            page = self._page(locator)
            lowered = page.text.lower()
            for term in terms:
                position = lowered.find(term)
                if position < 0:
                    continue
                start = position
                end = min(len(page.text), start + max(MAX_HIT_CHARS, len(term)))
                hits.append(
                    CaseHit(
                        case_id=case_id,
                        locator=locator,
                        quote=page.text[start:end],
                        start=start,
                        end=end,
                        score=float(len(term)),
                        source=SourceAnchor(
                            source=f"lexvault-bundle:{case_id}",
                            version=self.bundle.version,
                            locator=locator,
                        ),
                    )
                )
                break
        hits.sort(key=lambda hit: (-hit.score, view.page_locators.index(hit.locator)))
        return tuple(hits[:limit])

    def page_drafts(self, context: RunContext, case_id: str) -> tuple[tuple[str, RecordDraft], ...]:
        view = self.case(context, case_id)
        drafts: list[tuple[str, RecordDraft]] = []
        for locator in view.page_locators:
            if f"case:{case_id}/{locator}" not in context.scope.resources:
                continue
            page = self._page(locator)
            resource_id = f"case:{case_id}/{locator}"
            drafts.append(
                (
                    resource_id,
                    RecordDraft(
                        resource_id=resource_id,
                        kind=f"case_page:{view.title}",
                        content=page.text,
                        sources=(
                            SourceAnchor(
                                source=f"lexvault-bundle:{case_id}",
                                version=_page_version(page.text),
                                locator=locator,
                            ),
                        ),
                    ),
                )
            )
        return tuple(drafts)

    def resources(self, context: RunContext, case_id: str) -> tuple[str, ...]:
        view = self.case(context, case_id)
        return (
            f"case:{case_id}",
            *(f"case:{case_id}/{locator}" for locator in view.page_locators),
        )

    def iter_pages(self, context: RunContext, case_id: str) -> Iterable[tuple[str, CasePage]]:
        view = self.case(context, case_id)
        for locator in view.page_locators:
            if f"case:{case_id}/{locator}" in context.scope.resources:
                yield locator, self.page(context, case_id, locator)

    def _page(self, locator: str) -> BundlePage:
        page = next((page for page in self.bundle.pages if page.locator == locator), None)
        if page is None:
            raise CaseUnavailable("page unavailable for this case")
        return page


def _page_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


__all__ = ["MAX_QUERY_CHARS", "BundleCaseAdapter"]

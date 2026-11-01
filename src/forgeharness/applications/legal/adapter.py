"""Case material adapter for the legal read-only pilot (plan phase S3).

A host connects its own case repository by implementing :class:`CaseAdapter`. The
bundled :class:`SyntheticCaseAdapter` reads the repository's synthetic fixtures only;
it never touches a real matter, and it is explicit about that in every response.

The adapter is a *read* surface: it resolves the caller's case, returns page-level
text with locators, and can turn pages into revision drafts so the existing
review/publication/citation machinery owns what a reader is allowed to see. Nothing
here publishes anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from pydantic import Field

from forgeharness.sdk import FrozenModel, RecordDraft, RunContext, ScopeError, SourceAnchor

MAX_PAGE_CHARS = 20_000
MAX_QUERY_CHARS = 500
_LOCATOR = re.compile(r"^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9._-]{1,64}$")


class CaseUnavailable(ScopeError):
    """The case, its pages or a locator is not available to this caller."""


class CasePage(FrozenModel):
    """One locatable page of the case. ``content`` is untrusted reference material."""

    locator: str = Field(min_length=1, max_length=100)
    summary: str = Field(default="", max_length=300)
    content: str = Field(min_length=1, max_length=MAX_PAGE_CHARS)


class CaseView(FrozenModel):
    case_id: str = Field(min_length=1, max_length=100)
    tenant_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    page_locators: tuple[str, ...]
    synthetic: bool = True


class CaseHit(FrozenModel):
    """A page-level search result. The quote is an exact span of the page text."""

    case_id: str
    locator: str
    quote: str = Field(min_length=1, max_length=2_000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    score: float
    source: SourceAnchor


class CaseAdapter(Protocol):
    """Host-supplied case repository. Must authorize before reading anything."""

    def cases(self, context: RunContext) -> tuple[str, ...]: ...

    def case(self, context: RunContext, case_id: str) -> CaseView: ...

    def page(self, context: RunContext, case_id: str, locator: str) -> CasePage: ...

    def search(
        self, context: RunContext, case_id: str, query: str, *, limit: int = 5
    ) -> tuple[CaseHit, ...]: ...

    def page_drafts(
        self, context: RunContext, case_id: str
    ) -> tuple[tuple[str, RecordDraft], ...]: ...


class SyntheticCaseAdapter:
    """Reads ``<root>/<case_id>/case.json`` plus its page files. Synthetic fixtures only."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError("case fixture root does not exist")
        self._cases: dict[str, CaseView] = {}
        self._pages: dict[str, dict[str, CasePage]] = {}
        self._tenants: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        for directory in sorted(self.root.iterdir()):
            manifest_path = directory / "case.json"
            if not manifest_path.is_file():
                continue
            if not manifest_path.resolve().is_relative_to(self.root):
                raise ValueError("case manifest is outside the fixture root")
            manifest = json.loads(manifest_path.read_text())
            case_id = str(manifest["case_id"])
            if case_id != directory.name:
                raise ValueError(f"case directory does not match its case_id: {directory.name}")
            pages: dict[str, CasePage] = {}
            for entry in manifest["pages"]:
                locator = str(entry["locator"])
                if not _LOCATOR.match(locator):
                    raise ValueError(f"invalid page locator: {locator}")
                if locator in pages:
                    raise ValueError("duplicate page locator")
                page_path = (directory / entry["file"]).resolve()
                if not page_path.is_relative_to(directory.resolve()):
                    raise ValueError("page is outside its case directory")
                text = page_path.read_text().strip()
                if not text or len(text) > MAX_PAGE_CHARS:
                    raise ValueError(f"page content out of bounds: {entry['file']}")
                pages[locator] = CasePage(
                    locator=locator,
                    summary=str(entry.get("summary", "")),
                    content=text,
                )
            if not pages:
                raise ValueError(f"case has no pages: {case_id}")
            self._cases[case_id] = CaseView(
                case_id=case_id,
                tenant_id=str(manifest["tenant_id"]),
                title=str(manifest["title"]),
                page_locators=tuple(pages),
            )
            self._pages[case_id] = pages
            self._tenants[case_id] = str(manifest["tenant_id"])

    def cases(self, context: RunContext) -> tuple[str, ...]:
        """Case ids this caller may read: same tenant and an authorized case resource."""
        return tuple(
            case_id
            for case_id, view in self._cases.items()
            if view.tenant_id == context.scope.tenant_id and self._authorized(context, case_id)
        )

    def _authorized(self, context: RunContext, case_id: str) -> bool:
        if self._cases.get(case_id) is None:
            return False
        if self._tenants[case_id] != context.scope.tenant_id:
            return False
        resources = set(context.scope.resources)
        return f"case:{case_id}" in resources

    def case(self, context: RunContext, case_id: str) -> CaseView:
        if not self._authorized(context, case_id):
            # Missing, other-tenant and unauthorized cases share one answer, so a caller
            # cannot probe which case ids exist elsewhere.
            raise CaseUnavailable("case unavailable for this binding")
        return self._cases[case_id]

    def page(self, context: RunContext, case_id: str, locator: str) -> CasePage:
        view = self.case(context, case_id)
        if (
            locator not in view.page_locators
            or f"case:{case_id}/{locator}" not in context.scope.resources
        ):
            raise CaseUnavailable("page unavailable for this case")
        return self._pages[case_id][locator]

    def search(
        self, context: RunContext, case_id: str, query: str, *, limit: int = 5
    ) -> tuple[CaseHit, ...]:
        """Deterministic literal search inside one authorized case.

        Deliberately simple: this is a synthetic pilot, and a scored semantic ranking
        would claim retrieval quality the framework cannot prove. Terms are matched
        case-insensitively; ties break on page order, so results are reproducible.
        """
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be a bounded non-empty string")
        view = self.case(context, case_id)
        terms = [term for term in re.split(r"\W+", query.lower()) if len(term) > 2]
        if not terms:
            raise ValueError("query has no searchable terms")
        hits: list[CaseHit] = []
        for locator in view.page_locators:
            if f"case:{case_id}/{locator}" not in context.scope.resources:
                continue
            page = self._pages[case_id][locator]
            lowered = page.content.lower()
            for term in terms:
                position = lowered.find(term)
                if position < 0:
                    continue
                start, end = _span(page.content, position, len(term))
                hits.append(
                    CaseHit(
                        case_id=case_id,
                        locator=locator,
                        quote=page.content[start:end],
                        start=start,
                        end=end,
                        score=float(len(term)),
                        source=SourceAnchor(
                            source=f"synthetic-case:{case_id}",
                            version=_page_version(page),
                            locator=locator,
                        ),
                    )
                )
                break
        hits.sort(key=lambda hit: (-hit.score, view.page_locators.index(hit.locator)))
        return tuple(hits[:limit])

    def resources(self, context: RunContext, case_id: str) -> tuple[str, ...]:
        """Resources the host must grant for a complete read of this case."""
        view = self.case(context, case_id)
        return (
            f"case:{case_id}",
            *(f"case:{case_id}/{locator}" for locator in view.page_locators),
        )

    def page_drafts(self, context: RunContext, case_id: str) -> tuple[tuple[str, RecordDraft], ...]:
        """Revision drafts, one per page, ready for the host's review/publication chain.

        Publishing stays a host decision: these drafts carry the page locator as a
        source anchor, so a citation into the published revision can be tied back to
        the page a reader can open.
        """
        view = self.case(context, case_id)
        self.resources(context, case_id)
        drafts: list[tuple[str, RecordDraft]] = []
        for locator in view.page_locators:
            if f"case:{case_id}/{locator}" not in context.scope.resources:
                continue
            page = self._pages[case_id][locator]
            drafts.append(
                (
                    f"case:{case_id}/{locator}",
                    RecordDraft(
                        resource_id=f"case:{case_id}/{locator}",
                        kind=f"case_page:{view.title}",
                        content=page.content,
                        sources=(
                            SourceAnchor(
                                source=f"synthetic-case:{case_id}",
                                version=_page_version(page),
                                locator=locator,
                            ),
                        ),
                    ),
                )
            )
        return tuple(drafts)

    def iter_pages(self, context: RunContext, case_id: str) -> Iterable[tuple[str, CasePage]]:
        view = self.case(context, case_id)
        for locator in view.page_locators:
            if f"case:{case_id}/{locator}" in context.scope.resources:
                yield locator, self.page(context, case_id, locator)


def _page_version(page: CasePage) -> str:
    return hashlib.sha256(page.content.encode()).hexdigest()[:16]


def _span(content: str, position: int, length: int) -> tuple[int, int]:
    """Widen a literal hit to whole words so a quote is readable and still exact."""
    start = position
    while start > 0 and content[start - 1].isalnum():
        start -= 1
    end = position + length
    while end < len(content) and content[end].isalnum():
        end += 1
    return start, end


__all__ = [
    "CaseAdapter",
    "CaseHit",
    "CasePage",
    "CaseUnavailable",
    "CaseView",
    "SyntheticCaseAdapter",
]

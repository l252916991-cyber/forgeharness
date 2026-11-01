"""Sourced commerce FAQ knowledge: strict parsing, conflict detection, publication.

Publication goes through ``RevisionStore`` directly rather than through
``ImportService`` (ADR 015). The staged-import chain exists for *untrusted material*
(a PDF, an image) that has to be staged, parsed and validated before any draft may
exist. Here the corpus is a host-owned JSON fixture that this module already parses
and validates deterministically, and the import chain would add a staging database, a
leased blob directory and an optional ``TaskStore`` without adding a guarantee this
routine needs: ``RevisionStore.save`` already returns the existing revision for a
repeated host request identity, ``review``/``publish`` already enforce independent
review and exact-revision publication, and the head version already provides the
compare-and-set. So one FAQ is one draft revision, reviewed and published by three
distinct host contexts, with the request identity derived from the exact draft
payload: re-running the publication writes nothing.

A revision's kind is ``commerce_faq`` and its resource id is ``faq:{id}``, which must
be present in a reader's ``scope.resources`` before any read is authorized. A conflict
is only a conflict when both entries declare it, so an escalation can be driven by
``conflicting_faq_ids`` instead of by guessing from prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from forgeharness.sdk import (
    ContentRevision,
    FrozenModel,
    RecordDraft,
    RecordUnavailable,
    RevisionStore,
    RunContext,
    SourceAnchor,
    anchors_for,
)

FAQ_KIND = "commerce_faq"
FAQ_RESOURCE_PREFIX = "faq:"
FAQ_ID_PATTERN = r"^[a-z][a-z0-9-]{2,63}$"
# A rendered anchor must survive ``SourceRef.from_anchor``: the locator is bounded at
# 200 characters and may not contain the delimiters the anchor form reserves.
MAX_LOCATOR_CHARS = 200
_FAQ_ID = re.compile(FAQ_ID_PATTERN)


class KnowledgeError(ValueError):
    """A FAQ document, corpus or publication request is not valid business input."""


class FaqEntry(FrozenModel):
    """One validated FAQ fixture document, including its declared sources."""

    schema_version: Literal[1] = 1
    id: str = Field(pattern=FAQ_ID_PATTERN)
    tenant_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=20_000)
    category: str = Field(min_length=1, max_length=100)
    sources: tuple[SourceAnchor, ...] = Field(min_length=1, max_length=20)
    conflict_with: str | None = Field(default=None, pattern=FAQ_ID_PATTERN)

    @model_validator(mode="after")
    def validate_entry(self) -> FaqEntry:
        """Refuse a self-conflict, or a source that cannot render as an anchor."""
        if self.conflict_with == self.id:
            raise ValueError("a FAQ cannot declare a conflict with itself")
        for source in self.sources:
            locator = source.locator
            if len(locator) > MAX_LOCATOR_CHARS or "@" in locator or "#" in locator:
                raise ValueError("FAQ source locator must render as one anchor locator")
        return self


def faq_resource_id(faq_id: str) -> str:
    """The scoped record identity of one FAQ: ``faq:{id}``."""
    if not isinstance(faq_id, str) or _FAQ_ID.fullmatch(faq_id) is None:
        raise KnowledgeError("FAQ id must match the fixture id pattern")
    return f"{FAQ_RESOURCE_PREFIX}{faq_id}"


def parse_faq(document: object) -> FaqEntry:
    """Validate one already-parsed FAQ document against the fixture schema.

    Unknown keys, a foreign schema version, a blank field, a self-conflict and an
    unrenderable source locator are all refused rather than repaired.
    """
    if not isinstance(document, Mapping):
        raise KnowledgeError("a FAQ document must be a JSON object")
    try:
        return FaqEntry.model_validate(dict(document))
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "<document>"
        raise KnowledgeError(f"invalid FAQ document at {where}: {first['msg']}") from error


def load_faq(path: Path) -> FaqEntry:
    """Parse one FAQ file, requiring its ``id`` to match its file name."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise KnowledgeError(f"FAQ file is unreadable: {path}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise KnowledgeError("FAQ file is not valid JSON") from error
    entry = parse_faq(document)
    if entry.id != Path(path).stem:
        raise KnowledgeError(f"FAQ id {entry.id!r} does not match its file name")
    return entry


def validate_faqs(entries: Iterable[FaqEntry]) -> tuple[FaqEntry, ...]:
    """Check the cross-entry invariants and return the corpus sorted by id.

    Ids are unique, every ``conflict_with`` resolves to a known entry, the declaration
    is symmetric in both directions, and a conflict never crosses a tenant boundary.
    """
    validated: list[FaqEntry] = []
    known: dict[str, FaqEntry] = {}
    for item in entries:
        entry = FaqEntry.model_validate(item)
        if entry.id in known:
            raise KnowledgeError(f"duplicate FAQ id: {entry.id}")
        known[entry.id] = entry
        validated.append(entry)
    for entry in validated:
        if entry.conflict_with is None:
            continue
        other = known.get(entry.conflict_with)
        if other is None:
            raise KnowledgeError(
                f"{entry.id} declares a conflict with the unknown FAQ {entry.conflict_with}"
            )
        if other.conflict_with != entry.id:
            raise KnowledgeError(f"{entry.id}/{other.id}: a conflict must be declared by both")
        if other.tenant_id != entry.tenant_id:
            raise KnowledgeError(f"{entry.id}/{other.id}: a conflict must stay inside one tenant")
    validated.sort(key=lambda entry: entry.id)
    return tuple(validated)


def load_faq_corpus(directory: Path, *, tenant_id: str | None = None) -> tuple[FaqEntry, ...]:
    """Load and validate every ``*.json`` FAQ in one directory, sorted by id."""
    root = Path(directory)
    paths = sorted(root.glob("*.json"))
    if not paths:
        raise KnowledgeError(f"FAQ corpus is empty: {directory}")
    corpus = validate_faqs(load_faq(path) for path in paths)
    if tenant_id is None:
        return corpus
    return tuple(entry for entry in corpus if entry.tenant_id == tenant_id)


def conflicting_faq_ids(entries: Iterable[FaqEntry]) -> tuple[tuple[str, str], ...]:
    """Mutually declared conflicts as sorted, de-duplicated id pairs.

    A one-sided ``conflict_with`` is not a conflict: both entries must point at each
    other and share a tenant. Each pair is normalised to ascending id order, so the
    two symmetric declarations collapse into exactly one tuple.
    """
    known = {entry.id: entry for entry in entries}
    pairs: set[tuple[str, str]] = set()
    for entry in known.values():
        if entry.conflict_with is None:
            continue
        other = known.get(entry.conflict_with)
        if other is None or other.conflict_with != entry.id:
            continue
        if other.tenant_id != entry.tenant_id:
            continue
        pairs.add((min(entry.id, other.id), max(entry.id, other.id)))
    return tuple(sorted(pairs))


def faq_content(entry: FaqEntry) -> str:
    """Canonical revision content of one FAQ: deterministic, sorted-key JSON."""
    return json.dumps(
        {
            "id": entry.id,
            "tenant_id": entry.tenant_id,
            "category": entry.category,
            "question": entry.question,
            "answer": entry.answer,
            "sources": [source.model_dump(mode="json") for source in entry.sources],
            "conflict_with": entry.conflict_with,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def faq_draft(entry: FaqEntry) -> RecordDraft:
    """The exact draft one FAQ publishes: one revision, sourced by its own locators."""
    return RecordDraft(
        resource_id=faq_resource_id(entry.id),
        kind=FAQ_KIND,
        content=faq_content(entry),
        sources=entry.sources,
    )


def faq_anchor(revision: ContentRevision) -> str:
    """The documented anchor of a published FAQ: ``resource_id@version#locator``.

    The locator is the FAQ's own first declared source locator, which is the provenance
    the entry was published with.
    """
    locators = tuple(source.locator for source in revision.sources) or ("",)
    return anchors_for(revision.resource_id, str(revision.version), locators)[0]


def read_faq(*, store: RevisionStore, context: RunContext, faq_id: str) -> ContentRevision:
    """Read the currently published revision of one scoped FAQ.

    The store authorizes the resource against ``context.scope.resources`` and the
    caller's tenant before any content is read, so an unscoped id raises ``ScopeError``
    and another tenant's id is simply unavailable.
    """
    return store.published(context, faq_resource_id(faq_id))


def _request_id(entry: FaqEntry) -> str:
    """Stable host request identity for the exact draft payload of one FAQ."""
    payload = json.dumps(
        faq_draft(entry).model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    return f"commerce-faq-{entry.id}-{digest[:32]}"


def _same_draft(revision: ContentRevision, draft: RecordDraft) -> bool:
    """True when a stored revision already is exactly this draft."""
    return (
        revision.resource_id == draft.resource_id
        and revision.kind == draft.kind
        and revision.content == draft.content
        and revision.sources == draft.sources
        and revision.dependencies == draft.dependencies
        and revision.expires_at == draft.expires_at
    )


def _current(store: RevisionStore, context: RunContext, resource_id: str) -> ContentRevision | None:
    """The head revision of one FAQ, or ``None`` when it has none yet."""
    try:
        return store.read(context, resource_id)
    except RecordUnavailable:
        return None


def _publication_tenant(editor: RunContext, reviewer: RunContext, publisher: RunContext) -> str:
    """One tenant and one application, three distinct actors, or nothing runs."""
    contexts = (editor, reviewer, publisher)
    if len({context.actor.id for context in contexts}) != 3:
        raise KnowledgeError("editor, reviewer and publisher must be three distinct actors")
    if len({context.application_id for context in contexts}) != 1:
        raise KnowledgeError("publication contexts must share one application")
    tenants = {context.scope.tenant_id for context in contexts}
    if len(tenants) != 1:
        raise KnowledgeError("publication contexts must share one tenant")
    return tenants.pop()


def _publish_one(
    *,
    store: RevisionStore,
    entry: FaqEntry,
    editor: RunContext,
    reviewer: RunContext,
    publisher: RunContext,
) -> ContentRevision:
    """Take one FAQ through draft, independent review and publication.

    A publication that already exists for this exact content is returned untouched, so
    a host retry cannot create a second revision. Changed content is a new version
    through the ordinary compare-and-set, never an edit of a published revision.
    """
    draft = faq_draft(entry)
    head = _current(store, editor, draft.resource_id)
    if head is not None and head.state == "published" and _same_draft(head, draft):
        return head
    revision = store.save(
        editor,
        draft,
        expected_version=head.version if head is not None else 0,
        request_id=_request_id(entry),
    )
    if revision.state == "published":
        return revision
    if revision.state == "rejected":
        raise KnowledgeError(
            f"{draft.resource_id}: a rejected revision must be superseded, not republished"
        )
    if revision.state == "draft":
        revision = store.review(
            reviewer, draft.resource_id, expected_version=revision.version, approve=True
        )
    if revision.state == "approved":
        revision = store.publish(publisher, draft.resource_id, expected_version=revision.version)
    return revision


def publish_faq(
    *,
    store: RevisionStore,
    entries: Iterable[FaqEntry],
    editor: RunContext,
    reviewer: RunContext,
    publisher: RunContext,
) -> tuple[ContentRevision, ...]:
    """Publish one revision per FAQ for the contexts' tenant, in id order.

    This is a host operation, never a model capability: the three contexts must belong
    to one application and tenant and must be three distinct actors, so no single
    identity can draft, approve and publish the same knowledge. Entries of another
    tenant are refused rather than skipped, because a silent partial publication is
    indistinguishable from a complete one at the call site.
    """
    corpus = validate_faqs(entries)
    tenant = _publication_tenant(editor, reviewer, publisher)
    selected = tuple(entry for entry in corpus if entry.tenant_id == tenant)
    if not selected:
        raise KnowledgeError("no FAQ entry belongs to the publishing tenant")
    if len(selected) != len(corpus):
        raise KnowledgeError("refusing to publish FAQ entries of another tenant")
    return tuple(
        _publish_one(
            store=store, entry=entry, editor=editor, reviewer=reviewer, publisher=publisher
        )
        for entry in selected
    )


__all__ = [
    "FAQ_ID_PATTERN",
    "FAQ_KIND",
    "FAQ_RESOURCE_PREFIX",
    "MAX_LOCATOR_CHARS",
    "FaqEntry",
    "KnowledgeError",
    "conflicting_faq_ids",
    "faq_anchor",
    "faq_content",
    "faq_draft",
    "faq_resource_id",
    "load_faq",
    "load_faq_corpus",
    "parse_faq",
    "publish_faq",
    "read_faq",
    "validate_faqs",
]

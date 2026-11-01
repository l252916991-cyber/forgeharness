"""Governed source references shared by records, tools, retrieval and sessions.

A reference identifies *where* content came from and *which version* was read. It never
carries the content, and holding one confers no permission to read the source again:
every consumer re-checks the reference against the host's current ledger (ADR 016).

The documented anchor form is ``resource_id@version#locator``. Tools that declared
``ToolSpec.governed_sources`` put one anchor per read into their receipt, which is what
lets a single result depend on several page-level resources at once.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import Field

from forgeharness.domain.models import BusinessReceipt, FrozenModel, ToolEvidence

# Deliberately strict: an unrecognised reference is not silently promoted into an
# unversioned dependency, it is simply not a governed anchor.
_ANCHOR = re.compile(
    r"(?P<resource_id>[^@#\x00-\x1f\x7f]{1,200})@"
    r"(?P<version>[^@#\x00-\x1f\x7f]{1,200})#"
    r"(?P<locator>[^@#\x00-\x1f\x7f]{0,200})"
)


class SourceRef(FrozenModel):
    """One version-bound source read, as an identifier rather than a copy."""

    resource_id: str = Field(min_length=1, max_length=200, pattern=r"^[^@#\x00-\x1f\x7f]+$")
    version: str = Field(min_length=1, max_length=200, pattern=r"^[^@#\x00-\x1f\x7f]+$")
    locator: str = Field(default="", max_length=200, pattern=r"^[^@#\x00-\x1f\x7f]*$")

    @classmethod
    def from_anchor(cls, anchor: str) -> SourceRef | None:
        """Parse a documented anchor, or return ``None`` when it is not one.

        ``None`` means "not a governed anchor". Callers must treat that as "no declared
        dependency", never as "current".
        """
        match = _ANCHOR.fullmatch(anchor)
        if match is None:
            return None
        return cls(**match.groupdict())

    @property
    def anchor(self) -> str:
        """The reference rendered in the documented anchor form."""
        return f"{self.resource_id}@{self.version}#{self.locator}"


def refs_from_receipt(receipt: BusinessReceipt) -> tuple[SourceRef, ...]:
    """Every resolvable anchor of one receipt, de-duplicated and in order."""
    found: list[SourceRef] = []
    for anchor in receipt.evidence_refs:
        ref = SourceRef.from_anchor(anchor)
        if ref is not None and ref not in found:
            found.append(ref)
    return tuple(found)


def governed_source_refs(
    evidence: Iterable[ToolEvidence], governed_tools: frozenset[str]
) -> tuple[SourceRef, ...]:
    """Validate governed receipts and collect every declared read dependency.

    Success requires versioned anchors, except for an explicitly empty result:
    empty content, no receipt, and exactly ``{"source_empty": True}`` metadata.
    Failure receipts still contribute dependencies; a tool may fail after a read.
    An unrelated tool's receipt is execution evidence, not a governed source.
    """
    found: list[SourceRef] = []
    for item in evidence:
        if item.name not in governed_tools:
            continue
        if item.receipt is None:
            if not item.ok or (item.content == "" and item.metadata == {"source_empty": True}):
                continue
            raise ValueError("governed tool result is missing source evidence")
        refs = refs_from_receipt(item.receipt)
        if not refs or any(SourceRef.from_anchor(a) is None for a in item.receipt.evidence_refs):
            raise ValueError("governed tool result has invalid or missing source anchors")
        for ref in refs:
            if ref not in found:
                found.append(ref)
    return tuple(found)


def anchors_for(resource_id: str, version: str, locators: Iterable[str]) -> tuple[str, ...]:
    """Render one anchor per locator, in order, without duplicates."""
    rendered: list[str] = []
    for locator in locators:
        anchor = SourceRef(resource_id=resource_id, version=version, locator=locator).anchor
        if anchor not in rendered:
            rendered.append(anchor)
    return tuple(rendered)


__all__ = ["SourceRef", "anchors_for", "governed_source_refs", "refs_from_receipt"]

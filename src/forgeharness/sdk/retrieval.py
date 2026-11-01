"""Version-bound local retrieval over published, reviewed revisions.

Every index row is bound to one published revision identity
``(application, tenant, resource, version, digest)`` and one exact content span.
A search derives its candidate set from the caller's current ``RunContext`` *before*
any scoring, then re-reads each candidate's currently published revision and asks the
host's ``validate_sources`` callback again before a hit is returned. A stale,
withdrawn, expired or denied candidate is dropped instead of served; a stale row is
also removed so it cannot be exposed repeatedly. Nothing is ever returned, or
substituted, from an unvalidated row.

Scoring is a local and deterministic BM25-like term overlap, never a semantic or
learned ranking. Over at most ``MAX_CANDIDATES`` rows that are all authorized for the
caller:

    score = sum over distinct query terms t that occur in the entry of
            idf(t) * f(t) * (k1 + 1) / (f(t) + k1 * (1 - b + b * length / average))
    idf(t) = ln(1 + (N - n(t) + 0.5) / (n(t) + 0.5))

``N`` is the number of authorized candidate rows, ``n(t)`` how many of them contain
``t``, ``f(t)`` the occurrences of ``t`` in the entry, ``length`` its token count and
``average`` the mean token count of the candidates (``k1 = 1.2``, ``b = 0.75``).
Terms are lowercased ``[\\w\\u4e00-\\u9fff]+`` tokens (the same shape the optional
knowledge index uses, so CJK text tokenizes the same way); only distinct query terms
score and they are capped at ``MAX_QUERY_TERMS``. Entries without term overlap are
never hits. Equal scores are ordered by ``(resource_id, version, locator, start,
end)``, so repeating an identical search returns an identical tuple.
"""

from __future__ import annotations

import math
import re
import sqlite3
import stat
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from forgeharness.domain.models import FrozenModel, RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.privacy import EvidenceTransform
from forgeharness.sdk.records import (
    ContentRevision,
    RecordConflict,
    RecordUnavailable,
    RevisionStore,
)
from forgeharness.sdk.sources import SourceRef
from forgeharness.state.sqlite import connect_wal

# Explicit bounds. A query, a host-provided span and a scoped resource list are all
# untrusted at this boundary, so each is refused rather than truncated when it
# exceeds the documented limit.
MAX_QUERY_CHARS = 4_000
MAX_CONTENT_CHARS = 100_000  # the same bound RecordDraft.content already carries
MAX_QUERY_TERMS = 64
MAX_CANDIDATES = 500  # authorized rows one search may score
MAX_VERIFY_BUDGET = 100  # published-revision re-checks one search may perform
MAX_SCOPE_RESOURCES = 500  # scoped resources one search may consider
MAX_RESOURCE_CHARS = 200  # SourceRef.resource_id bound
MAX_LOCATOR_CHARS = 200  # SourceRef.locator bound
MAX_ENTITY_CHARS = 128  # application and tenant identity bound
BM25_K1 = 1.2
BM25_B = 0.75
_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+")


class RetrievalUnavailable(ScopeError):
    """Retrieval rows or their revision are unavailable to this caller."""


class IndexConflict(ValueError):
    """The index schema, or a stored entry, conflicts with this contract."""


class IndexEntry(FrozenModel):
    """One searchable span of one published revision.

    ``start``/``end`` are offsets into the published revision content and ``content``
    is exactly that span, so a hit can never quote text its revision does not contain.
    """

    application_id: str
    tenant_id: str
    resource_id: str
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    locator: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    content: str
    schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def validate_span(self) -> IndexEntry:
        """Refuse an entry whose text is not exactly the span it declares."""
        if self.end <= self.start:
            raise ValueError("index entry span must be nonempty")
        if len(self.content) != self.end - self.start:
            raise ValueError("index entry content must be exactly its declared span")
        return self


class IndexHit(FrozenModel):
    """One verified hit: the indexed span, its score and its governed source reference."""

    entry: IndexEntry
    score: float
    source: SourceRef


def _terms(text: str) -> tuple[str, ...]:
    """Lowercased local terms; the token shape matches the optional legacy index."""
    return tuple(_TOKEN.findall(text.lower()))


def _query_terms(query: str) -> tuple[str, ...]:
    """Distinct query terms in first-appearance order, capped at ``MAX_QUERY_TERMS``."""
    return tuple(dict.fromkeys(_terms(query)))[:MAX_QUERY_TERMS]


def _tie_break(entry: IndexEntry) -> tuple[str, int, str, int, int]:
    """Stable identity order for equal scores; two rows cannot share this key."""
    return (entry.resource_id, entry.version, entry.locator, entry.start, entry.end)


def _bounded_resource(value: object) -> bool:
    """True for a nonempty resource identity that fits the index and ``SourceRef``."""
    return isinstance(value, str) and 1 <= len(value) <= MAX_RESOURCE_CHARS


def _scoped(context: RunContext) -> tuple[str, ...]:
    """Resource identities of the current scope, sorted so every cap is deterministic.

    Identities too long for a governed ``SourceRef`` are not candidates: a hit must be
    reportable as a reference, so such a resource is never indexed or scored.
    """
    return tuple(sorted(item for item in set(context.scope.resources) if _bounded_resource(item)))


def _score(terms: tuple[str, ...], entries: Sequence[IndexEntry]) -> list[tuple[IndexEntry, float]]:
    """Rank authorized candidates by the documented BM25-like term overlap.

    The corpus statistics come from the authorized candidate rows only, so a search
    cannot learn anything about rows outside the caller's scope, and the same rows
    always produce the same scores.
    """
    if not entries:
        return []
    total = len(entries)
    documents = [(entry, _terms(entry.content)) for entry in entries]
    average = sum(len(tokens) for _, tokens in documents) / total
    if average <= 0:
        # Punctuation-only candidates have no length to normalize against; keeping
        # the ratio finite is all that matters because no query term can match them.
        average = 1.0
    sets = [frozenset(tokens) for _, tokens in documents]
    frequency = {term: sum(1 for tokens in sets if term in tokens) for term in terms}
    ranked: list[tuple[IndexEntry, float]] = []
    for entry, tokens in documents:
        counts = Counter(tokens)
        length = len(tokens)
        score = 0.0
        for term in terms:
            count = counts.get(term, 0)
            if not count:
                continue
            inverse = math.log(1.0 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += (
                inverse
                * count
                * (BM25_K1 + 1)
                / (count + BM25_K1 * (1.0 - BM25_B + BM25_B * length / average))
            )
        if score > 0:
            ranked.append((entry, score))
    ranked.sort(key=lambda item: (-item[1], _tie_break(item[0])))
    return ranked


class AuthorizedIndex:
    """Single-writer local index bound to published revision versions.

    ``store`` is the only authority for "currently published"; ``validate_sources`` is
    consulted per candidate and must query the host's current ledger. Scoring is
    deterministic and documented in this module's docstring. Every mutating call and
    every search runs in its own ``BEGIN IMMEDIATE`` transaction, and the parent
    directory must already be owner-private.
    """

    def __init__(
        self,
        path: Path,
        *,
        store: RevisionStore,
        validate_sources: Callable[[RunContext, tuple[SourceRef, ...]], bool],
        transform: EvidenceTransform | None = None,
    ) -> None:
        if not callable(validate_sources):
            raise ValueError("a live host source validator is required")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise ScopeError("retrieval directory must be owner-private (mode 0700)")
        self.path = path
        self.store = store
        self.validate_sources = validate_sources
        # Index exactly what the records it points at already contain. A host that
        # configured a stricter store transform must not be silently downgraded to
        # the default mask here, so the store's transform is the default.
        self.transform: EvidenceTransform = transform if transform is not None else store.transform
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS retrieval_schema (version INTEGER NOT NULL)")
            versions = db.execute("SELECT version FROM retrieval_schema").fetchall()
            if not versions:
                db.execute("INSERT INTO retrieval_schema VALUES(1)")
            elif [int(row["version"]) for row in versions] != [1]:
                raise IndexConflict("unsupported retrieval schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS retrieval_entries ("
                "application_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                "resource_id TEXT NOT NULL, version INTEGER NOT NULL, locator TEXT NOT NULL, "
                "span_start INTEGER NOT NULL, span_end INTEGER NOT NULL, digest TEXT NOT NULL, "
                "content TEXT NOT NULL, PRIMARY KEY(application_id,tenant_id,resource_id,"
                "version,locator,span_start,span_end))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS retrieval_resource ON retrieval_entries"
                "(application_id,tenant_id,resource_id)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(connect_wal(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _key(context: RunContext) -> tuple[str, str]:
        return context.application_id, context.scope.tenant_id

    def _binding(self, context: RunContext) -> tuple[str, str]:
        """Validate the trusted binding and refuse one that carries credentials."""
        RunContext.model_validate(context.model_dump())
        if self.transform.transform(context.model_dump(mode="json")) != context.model_dump(
            mode="json"
        ):
            raise RetrievalUnavailable("retrieval binding must not contain credentials")
        return self._key(context)

    def _requested(
        self, context: RunContext, resource_ids: tuple[str, ...] | None
    ) -> tuple[str, ...]:
        """The resources a refresh may rebuild, in deterministic order.

        An explicitly requested resource outside the current scope is a scope error,
        never a silent no-op; the default is the caller's own scope.
        """
        self._binding(context)
        if resource_ids is None:
            return _scoped(context)[:MAX_SCOPE_RESOURCES]
        requested = tuple(dict.fromkeys(resource_ids))
        if len(requested) > MAX_SCOPE_RESOURCES:
            raise ValueError("too many retrieval resources requested")
        for resource_id in requested:
            if not _bounded_resource(resource_id):
                raise ValueError("resource id must be a bounded nonempty identity")
            if resource_id not in context.scope.resources:
                raise RetrievalUnavailable("retrieval resource outside current scope")
        return requested

    def _bounded(self, entry: IndexEntry) -> IndexEntry:
        """Re-validate a host-provided entry against the documented identity bounds."""
        value = IndexEntry.model_validate(entry.model_dump())
        if not _bounded_resource(value.resource_id):
            raise ValueError("resource id must be a bounded nonempty identity")
        if not 1 <= len(value.application_id) <= MAX_ENTITY_CHARS:
            raise ValueError("application identity must be bounded and nonempty")
        if not 1 <= len(value.tenant_id) <= MAX_ENTITY_CHARS:
            raise ValueError("tenant identity must be bounded and nonempty")
        if len(value.locator) > MAX_LOCATOR_CHARS:
            raise ValueError("locator exceeds the maximum length")
        if len(value.content) > MAX_CONTENT_CHARS:
            raise ValueError("index content exceeds the maximum length")
        return value

    @staticmethod
    def _store(db: sqlite3.Connection, entry: IndexEntry) -> None:
        db.execute(
            "INSERT INTO retrieval_entries VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(application_id,tenant_id,resource_id,version,locator,"
            "span_start,span_end) DO UPDATE SET digest=excluded.digest, "
            "content=excluded.content",
            (
                entry.application_id,
                entry.tenant_id,
                entry.resource_id,
                entry.version,
                entry.locator,
                entry.start,
                entry.end,
                entry.digest,
                entry.content,
            ),
        )

    @staticmethod
    def _purge(db: sqlite3.Connection, key: tuple[str, str], resource_id: str) -> int:
        """Delete every row of one resource; returns how many rows were removed."""
        db.execute("PRAGMA secure_delete=ON")
        cursor = db.execute(
            "DELETE FROM retrieval_entries WHERE application_id=? AND tenant_id=? "
            "AND resource_id=?",
            (*key, resource_id),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _purge_row(db: sqlite3.Connection, rowid: int) -> None:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("DELETE FROM retrieval_entries WHERE rowid=?", (rowid,))

    def _delete_resource(self, context: RunContext, resource_id: str) -> int:
        with self._connect() as db:
            return self._purge(db, self._key(context), resource_id)

    def _replace(self, context: RunContext, record: ContentRevision) -> IndexEntry:
        """Publish the whole-record span for one current publication.

        Rows of that resource whose version or digest is no longer the published one
        are deleted in the same transaction; host-provided spans of the *current*
        revision are kept, so a refresh cannot silently discard page-level entries.
        """
        entry = self._bounded(
            IndexEntry(
                application_id=context.application_id,
                tenant_id=context.scope.tenant_id,
                resource_id=record.resource_id,
                version=record.version,
                digest=record.digest,
                locator="",
                start=0,
                end=len(record.content),
                content=record.content,
            )
        )
        with self._connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute(
                "DELETE FROM retrieval_entries WHERE application_id=? AND tenant_id=? "
                "AND resource_id=? AND (version<>? OR digest<>?)",
                (*self._key(context), entry.resource_id, entry.version, entry.digest),
            )
            self._store(db, entry)
        return entry

    def refresh(
        self, context: RunContext, *, resource_ids: tuple[str, ...] | None = None
    ) -> tuple[IndexEntry, ...]:
        """(Re)build index rows for the currently published revisions the caller may read.

        Each listed resource receives exactly one whole-record row for its current
        publication. A withdrawn, expired, superseded or deleted record contributes
        nothing, and its stale rows are removed, so a refresh is also a cleanup. A
        policy denial is not staleness: those rows stay for callers who may still read
        them, and every search re-checks authorization anyway.
        """
        resources = self._requested(context, resource_ids)
        entries: list[IndexEntry] = []
        for resource_id in resources:
            try:
                record = self.store.published(context, resource_id)
            except (RecordUnavailable, RecordConflict):
                self._delete_resource(context, resource_id)
                continue
            except ScopeError:
                continue
            entries.append(self._replace(context, record))
        return tuple(entries)

    def upsert(self, entry: IndexEntry) -> None:
        """Store one host-provided span bound to a published revision version.

        The entry is not trusted here or later: every search re-reads the revision the
        row names and refuses, then prunes, a row whose version, digest or span no
        longer matches. Content the configured transform would change is refused
        outright, so a credential is never written to the index and the recorded
        offsets cannot drift from the text they describe.
        """
        value = self._bounded(entry)
        if self.transform.transform(value.content) != value.content:
            raise ValueError("index entry content must be transformed before indexing")
        with self._connect() as db:
            self._store(db, value)

    def drop(self, context: RunContext, *, resource_id: str) -> int:
        """Remove every row of one scoped resource; returns the removed row count."""
        self._binding(context)
        if not _bounded_resource(resource_id):
            raise ValueError("resource id must be a bounded nonempty identity")
        if resource_id not in context.scope.resources:
            raise RetrievalUnavailable("retrieval resource outside current scope")
        return self._delete_resource(context, resource_id)

    def _candidates(self, db: sqlite3.Connection, context: RunContext) -> tuple[IndexEntry, ...]:
        """Authorized rows only, selected by application, tenant and resource scope.

        This is the gate that runs before scoring: a row of another tenant, another
        application or an unscoped resource is never read out of the table, so no
        query can surface it. A row that no longer satisfies the entry contract cannot
        be a candidate and is removed instead of being skipped on every search.
        """
        resources = _scoped(context)[:MAX_SCOPE_RESOURCES]
        if not resources:
            return ()
        rows = db.execute(
            "SELECT rowid AS stored_row, application_id, tenant_id, resource_id, version, "
            "locator, span_start, span_end, digest, content FROM retrieval_entries "
            "WHERE application_id=? AND tenant_id=? "
            f"AND resource_id IN ({','.join('?' * len(resources))}) "
            "ORDER BY resource_id, version, locator, span_start, span_end LIMIT ?",
            (context.application_id, context.scope.tenant_id, *resources, MAX_CANDIDATES),
        ).fetchall()
        entries: list[IndexEntry] = []
        for row in rows:
            try:
                entries.append(
                    IndexEntry(
                        application_id=str(row["application_id"]),
                        tenant_id=str(row["tenant_id"]),
                        resource_id=str(row["resource_id"]),
                        version=int(row["version"]),
                        locator=str(row["locator"]),
                        start=int(row["span_start"]),
                        end=int(row["span_end"]),
                        digest=str(row["digest"]),
                        content=str(row["content"]),
                    )
                )
            except (TypeError, ValueError):
                # A hand-edited or interrupted row is not a candidate.
                self._purge_row(db, int(row["stored_row"]))
        return tuple(entries)

    def _verify(
        self, db: sqlite3.Connection, context: RunContext, entry: IndexEntry, score: float
    ) -> IndexHit | None:
        """Re-check one candidate against the current publication and host permission.

        The row is only ever a claim. The store decides whether that claim is still
        the current published revision, and the host callback decides whether the
        caller may read it now; anything else drops the candidate.
        """
        try:
            record = self.store.published(context, entry.resource_id)
        except (RecordUnavailable, RecordConflict):
            # No current publication: every row of this resource is stale.
            self._purge(db, self._key(context), entry.resource_id)
            return None
        except ScopeError:
            # Permission was revoked. The rows may still be correct for another
            # caller, so drop the hit without destroying evidence.
            return None
        if (
            record.version != entry.version
            or record.digest != entry.digest
            or entry.end > len(record.content)
            or record.content[entry.start : entry.end] != entry.content
        ):
            self._purge(db, self._key(context), entry.resource_id)
            return None
        source = SourceRef(
            resource_id=entry.resource_id,
            version=str(entry.version),
            locator=entry.locator,
        )
        if self.validate_sources(context, (source,)) is not True:
            # Fail closed: a denied candidate is dropped, and no unvalidated row is
            # substituted for it, even when everything the caller asked for is denied.
            return None
        return IndexHit(entry=entry, score=score, source=source)

    def search(self, context: RunContext, query: str, *, limit: int = 5) -> tuple[IndexHit, ...]:
        """Return at most ``limit`` verified hits for the caller's current scope.

        Authorization precedes scoring: candidates come from the context's
        application, tenant and resource scope as SQL predicates, so an unauthorized
        row is never scored, never re-read and never counted in a result. Each scored
        candidate is then re-checked against the currently published revision
        (version, digest, exact span) and the host's ``validate_sources`` callback. A
        stale candidate is pruned, a denied one is only dropped, and a search whose
        candidates all fail returns ``()``.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("retrieval limit must be between 1 and 100")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("retrieval query must not be empty")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError("retrieval query exceeds the maximum length")
        terms = _query_terms(query)
        self._binding(context)
        if not terms:
            # A query of punctuation alone has no term that could match any content.
            return ()
        hits: list[IndexHit] = []
        with self._connect() as db:
            authorized = tuple(
                entry
                for entry in self._candidates(db, context)
                if self._verify(db, context, entry, 0.0) is not None
            )
            ranked = _score(terms, authorized)
            for entry, score in ranked[:MAX_VERIFY_BUDGET]:
                if len(hits) >= limit:
                    break
                hit = self._verify(db, context, entry, score)
                if hit is not None:
                    hits.append(hit)
        return tuple(hits)

    def count(self, context: RunContext, *, resource_id: str | None = None) -> int:
        """Index rows this caller's scope covers, or those of one scoped resource.

        This counts stored rows, not verified hits; it exists for host introspection
        and never counts another tenant's rows.
        """
        self._binding(context)
        key = self._key(context)
        params: tuple[object, ...]
        if resource_id is not None:
            if not _bounded_resource(resource_id):
                raise ValueError("resource id must be a bounded nonempty identity")
            if resource_id not in context.scope.resources:
                raise RetrievalUnavailable("retrieval resource outside current scope")
            query = (
                "SELECT count(*) FROM retrieval_entries WHERE application_id=? AND tenant_id=? "
                "AND resource_id=?"
            )
            params = (*key, resource_id)
        else:
            resources = _scoped(context)
            if not resources:
                return 0
            query = (
                "SELECT count(*) FROM retrieval_entries WHERE application_id=? AND tenant_id=? "
                f"AND resource_id IN ({','.join('?' * len(resources))})"
            )
            params = (*key, *resources)
        with self._connect() as db:
            return int(db.execute(query, params).fetchone()[0])

    def __len__(self) -> int:
        """Total stored rows, for host-level checks that are not caller-scoped."""
        with self._connect() as db:
            return int(db.execute("SELECT count(*) FROM retrieval_entries").fetchone()[0])

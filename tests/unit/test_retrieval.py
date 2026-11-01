"""Version-bound retrieval: authorization before scoring, staleness and determinism.

The tests use a real ``RevisionStore`` with ``RoleRecordPolicy`` and a synthetic host
validator that can be flipped, so every claim about "currently published" and "host
permission" is exercised against the real contracts rather than a stub.
"""

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from forgeharness.domain.models import Actor, ResourceScope, RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.privacy import CredentialMask
from forgeharness.sdk.records import RecordDraft, RevisionStore, RoleRecordPolicy
from forgeharness.sdk.retrieval import (
    MAX_CONTENT_CHARS,
    MAX_QUERY_CHARS,
    AuthorizedIndex,
    IndexConflict,
    IndexEntry,
    RetrievalUnavailable,
)
from forgeharness.sdk.sources import SourceRef

TEXT = "synthetic case evidence payment 付款 日期"


def context(
    role: str = "reader",
    *,
    resources: tuple[str, ...] = ("case:1",),
    tenant: str = "firm-a",
    application: str = "echo",
    actor: str | None = None,
) -> RunContext:
    return RunContext(
        application_id=application,
        application_version="1",
        policy_version="1",
        run_id="r1",
        session_id="s1",
        actor=Actor(id=actor or role, roles=(role,)),
        scope=ResourceScope(tenant_id=tenant, resources=resources),
        correlation_id="c1",
    )


class Validator:
    """Synthetic host ledger: records every checked reference and can deny by resource."""

    def __init__(self, *, allowed: bool = True, denied: frozenset[str] = frozenset()) -> None:
        self.allowed = allowed
        self.denied = denied
        self.calls: list[tuple[SourceRef, ...]] = []

    def __call__(self, context: RunContext, refs: tuple[SourceRef, ...]) -> bool:
        del context
        self.calls.append(refs)
        return self.allowed and refs[0].resource_id not in self.denied


def publish(
    store: RevisionStore,
    resource_id: str = "case:1",
    text: str = TEXT,
    *,
    key: str = "request-1",
    expires_at: datetime | None = None,
):
    resources = (resource_id,)
    store.save(
        context("editor", resources=resources),
        RecordDraft(resource_id=resource_id, kind="evidence", content=text, expires_at=expires_at),
        expected_version=0,
        request_id=key,
    )
    store.review(
        context("reviewer", resources=resources), resource_id, expected_version=1, approve=True
    )
    return store.publish(context("publisher", resources=resources), resource_id, expected_version=1)


def republish(store: RevisionStore, resource_id: str, text: str, *, key: str):
    resources = (resource_id,)
    store.save(
        context("editor", resources=resources),
        RecordDraft(resource_id=resource_id, kind="evidence", content=text),
        expected_version=1,
        request_id=key,
    )
    store.review(
        context("reviewer", resources=resources), resource_id, expected_version=2, approve=True
    )
    return store.publish(context("publisher", resources=resources), resource_id, expected_version=2)


def entry_for(record, *, locator: str = "", start: int = 0, end: int | None = None, **changes):
    end = len(record.content) if end is None else end
    values = {
        "application_id": "echo",
        "tenant_id": "firm-a",
        "resource_id": record.resource_id,
        "version": record.version,
        "digest": record.digest,
        "locator": locator,
        "start": start,
        "end": end,
        "content": record.content[start:end],
    }
    values.update(changes)
    return IndexEntry(**values)


def build(tmp_path, *, store=None, validator=None):
    store = store or RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    validator = validator if validator is not None else Validator()
    index = AuthorizedIndex(tmp_path / "retrieval.db", store=store, validate_sources=validator)
    return store, validator, index


def test_refresh_indexes_only_the_current_publication(tmp_path):
    store, _, index = build(tmp_path)
    store.save(
        context("editor"),
        RecordDraft(resource_id="case:1", kind="evidence", content=TEXT),
        expected_version=0,
        request_id="draft-only",
    )
    # A draft is not material: refresh contributes nothing and stores no row.
    assert index.refresh(context("reader")) == ()
    assert len(index) == 0
    store.review(context("reviewer"), "case:1", expected_version=1, approve=True)
    record = store.publish(context("publisher"), "case:1", expected_version=1)
    entries = index.refresh(context("reader"))
    assert len(entries) == 1
    entry = entries[0]
    assert (entry.resource_id, entry.version, entry.digest) == ("case:1", 1, record.digest)
    assert (entry.locator, entry.start, entry.end) == ("", 0, len(record.content))
    assert entry.content == record.content
    assert (entry.schema_version, index.count(context("reader"))) == (1, 1)


def test_search_returns_version_bound_hits_with_governed_references(tmp_path):
    store, validator, index = build(tmp_path)
    record = publish(store)
    index.refresh(context("reader"))
    hits = index.search(context("reader"), "payment")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.entry.content == record.content
    assert hit.score > 0
    assert hit.source == SourceRef(resource_id="case:1", version="1", locator="")
    assert hit.source.anchor == "case:1@1#"
    assert validator.calls == [(hit.source,), (hit.source,)]


def test_other_tenant_rows_are_never_read_or_returned(tmp_path, monkeypatch):
    store, validator, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    reads: list[str] = []
    original = store.published

    def counted(caller, resource_id):
        reads.append(resource_id)
        return original(caller, resource_id)

    monkeypatch.setattr(store, "published", counted)
    other = context("reader", tenant="firm-b")
    assert index.search(other, "payment") == ()
    assert index.count(other) == 0
    # Authorization precedes scoring: no candidate row of another tenant is even read,
    # let alone scored or verified.
    assert reads == []
    assert validator.calls == []
    assert len(index) == 1


def test_unscoped_resources_are_not_candidates(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, "case:1", "synthetic alpha one", key="a")
    publish(store, "case:2", "synthetic alpha two", key="b")
    wide = context("reader", resources=("case:1", "case:2"))
    assert len(index.refresh(wide)) == 2
    narrow = context("reader", resources=("case:2",))
    hits = index.search(narrow, "alpha")
    assert [hit.entry.resource_id for hit in hits] == ["case:2"]
    assert index.count(narrow) == 1
    empty = context("reader", resources=())
    assert index.search(empty, "alpha") == ()
    assert index.count(empty) == 0


def test_application_mismatch_is_not_a_candidate(tmp_path):
    store, _, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    stranger = context("reader", application="other-app")
    assert index.search(stranger, "payment") == ()
    assert index.count(stranger) == 0
    assert len(index) == 1


def test_scope_removal_hides_rows_that_are_still_stored(tmp_path):
    store, _, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    assert len(index.search(context("reader"), "payment")) == 1
    narrowed = context("reader", resources=("case:2",))
    assert index.search(narrowed, "payment") == ()
    assert len(index) == 1


def test_withdrawal_stops_exposure_and_removes_the_row(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, text="synthetic alpha withdrawn-only marker")
    index.refresh(context("reader"))
    assert len(index.search(context("reader"), "withdrawn")) == 1
    store.withdraw(context("publisher"), "case:1", expected_version=1)
    assert index.search(context("reader"), "withdrawn") == ()
    assert index.count(context("reader")) == 0
    # S2 acceptance: 资料撤回后检索不能继续暴露旧内容, and the stale row is gone.
    assert len(index) == 0


def test_superseding_version_hides_old_text_and_prunes(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, text="synthetic alpha old-only marker", key="first")
    index.refresh(context("reader"))
    assert len(index.search(context("reader"), "old")) == 1
    republish(store, "case:1", "synthetic beta new-only marker", key="second")
    assert index.search(context("reader"), "old") == ()
    assert len(index) == 0
    entries = index.refresh(context("reader"))
    assert [entry.version for entry in entries] == [2]
    hits = index.search(context("reader"), "beta")
    assert [hit.entry.version for hit in hits] == [2]


def test_record_deletion_prunes_index_rows(tmp_path):
    store, _, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    store.delete(context("admin"), "case:1")
    assert index.search(context("reader"), "payment") == ()
    assert len(index) == 0


@pytest.mark.parametrize(
    ("version", "digest"),
    [(2, "b" * 64), (1, "c" * 64), (1, "not-a-digest")],
)
def test_stale_recorded_version_or_digest_is_never_served(tmp_path, version, digest):
    store, _, index = build(tmp_path)
    record = publish(store)
    if digest == "not-a-digest":
        # A hand-written row cannot even satisfy the entry contract: it is removed
        # instead of being skipped forever.
        with sqlite3.connect(tmp_path / "retrieval.db") as db:
            db.execute(
                "INSERT INTO retrieval_entries VALUES(?,?,?,?,?,?,?,?,?)",
                ("echo", "firm-a", "case:1", 1, "", 0, 5, digest, "synth"),
            )
    else:
        index.upsert(entry_for(record, version=version, digest=digest))
    assert len(index) == 1
    assert index.search(context("reader"), "synthetic") == ()
    assert len(index) == 0


def test_host_validator_denial_is_never_bypassed(tmp_path):
    validator = Validator(allowed=False)
    store, _, index = build(tmp_path, validator=validator)
    publish(store)
    index.refresh(context("reader"))
    # Everything the caller asked for is denied: the result is empty, not unvalidated.
    assert index.search(context("reader"), "payment") == ()
    assert [refs[0].resource_id for refs in validator.calls] == ["case:1"]
    assert validator.calls[0][0].version == "1"
    validator.allowed = True
    assert len(index.search(context("reader"), "payment")) == 1


def test_partial_host_validator_denial_drops_only_that_candidate(tmp_path):
    validator = Validator()
    store, _, index = build(tmp_path, validator=validator)
    publish(store, "case:1", "synthetic alpha one", key="a")
    publish(store, "case:2", "synthetic alpha two", key="b")
    reader = context("reader", resources=("case:1", "case:2"))
    index.refresh(reader)
    validator.denied = frozenset({"case:1"})
    hits = index.search(reader, "alpha")
    assert [hit.entry.resource_id for hit in hits] == ["case:2"]
    # The denied row is fresh and still stored; it is dropped on permission only.
    assert index.count(reader) == 2


def test_revoked_permission_drops_hits_without_destroying_rows(tmp_path):
    class Policy(RoleRecordPolicy):
        revoked = False

        def authorize(self, context, operation, resource_id):
            return not self.revoked and super().authorize(context, operation, resource_id)

    policy = Policy()
    store = RevisionStore(tmp_path / "records.db", policy=policy)
    _, _, index = build(tmp_path, store=store)
    publish(store)
    index.refresh(context("reader"))
    assert len(index.search(context("reader"), "payment")) == 1
    policy.revoked = True
    assert index.search(context("reader"), "payment") == ()
    assert index.refresh(context("reader")) == ()
    assert len(index) == 1
    policy.revoked = False
    assert len(index.search(context("reader"), "payment")) == 1


def test_expired_publication_stops_exposure_and_is_pruned(tmp_path):
    store, _, index = build(tmp_path)
    publish(
        store,
        text="synthetic alpha expiring",
        expires_at=datetime.now(UTC) + timedelta(seconds=0.75),
    )
    index.refresh(context("reader"))
    assert len(index.search(context("reader"), "alpha")) == 1
    time.sleep(0.85)
    assert index.search(context("reader"), "alpha") == ()
    assert len(index) == 0


def test_transform_masks_credentials_in_indexed_and_hit_content(tmp_path):
    store, _, index = build(tmp_path)
    record = publish(store, text="synthetic case api_key=example-sensitive-value payment")
    assert isinstance(index.transform, CredentialMask)
    entries = index.refresh(context("reader"))
    assert "example-sensitive" not in record.content
    assert "[REDACTED]" in entries[0].content
    hits = index.search(context("reader"), "payment")
    assert "[REDACTED]" in hits[0].entry.content
    # The raw credential is not in the index text, so it cannot be matched.
    assert index.search(context("reader"), "example-sensitive") == ()
    raw = "synthetic case api_key=secret-value"
    with pytest.raises(ValueError, match="transformed"):
        index.upsert(entry_for(record, start=0, end=len(raw), content=raw))


def test_offsets_match_the_current_published_content(tmp_path):
    store, _, index = build(tmp_path)
    record = publish(store, text="synthetic alpha evidence beta")
    entry = index.refresh(context("reader"))[0]
    assert entry.content == record.content[entry.start : entry.end]
    # Tampering with the stored text (same length, so the row still loads) is refused
    # byte-for-byte and the row is pruned.
    with sqlite3.connect(tmp_path / "retrieval.db") as db:
        db.execute(
            "UPDATE retrieval_entries SET content=? WHERE resource_id='case:1'",
            ("synthetic XXXXX evidence beta",),
        )
    assert index.search(context("reader"), "synthetic") == ()
    assert len(index) == 0


def test_upserted_span_beyond_the_revision_is_refused(tmp_path):
    store, _, index = build(tmp_path)
    record = publish(store, text="synthetic alpha")
    oversize = "x" * (len(record.content) + 1)
    index.upsert(entry_for(record, locator="page:9", start=0, end=len(oversize), content=oversize))
    assert index.search(context("reader"), oversize) == ()
    assert len(index) == 0


def test_upserted_sub_span_inside_the_revision_is_served(tmp_path):
    store, _, index = build(tmp_path)
    record = publish(store, text="prefix synthetic alpha suffix")
    start = record.content.index("synthetic")
    end = start + len("synthetic alpha")
    index.upsert(entry_for(record, locator="page:3", start=start, end=end))
    hits = index.search(context("reader"), "alpha")
    assert [hit.entry.locator for hit in hits] == ["page:3"]
    assert hits[0].entry.content == record.content[start:end]
    assert hits[0].source == SourceRef(resource_id="case:1", version="1", locator="page:3")


def test_repeated_search_is_deterministic_and_limit_is_bounded(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, "case:1", "synthetic alpha alpha evidence", key="a")
    publish(store, "case:2", "synthetic alpha evidence beta", key="b")
    reader = context("reader", resources=("case:1", "case:2"))
    index.refresh(reader)
    first = index.search(reader, "alpha evidence")
    assert first == index.search(reader, "alpha evidence")
    assert [hit.entry.resource_id for hit in first] == ["case:1", "case:2"]
    assert len(index.search(reader, "alpha", limit=1)) == 1
    assert index.search(reader, "alpha", limit=100) == index.search(reader, "alpha")
    for bad in (0, 101, True, "5", None):
        with pytest.raises(ValueError, match="limit"):
            index.search(reader, "alpha", limit=bad)


def test_equal_scores_use_a_stable_identity_tie_break(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, "case:1", "synthetic alpha tie", key="a")
    publish(store, "case:2", "synthetic alpha tie", key="b")
    reader = context("reader", resources=("case:2", "case:1"))
    index.refresh(reader)
    hits = index.search(reader, "alpha")
    assert len(hits) == 2
    assert hits[0].score == hits[1].score
    assert [hit.entry.resource_id for hit in hits] == ["case:1", "case:2"]


def test_chinese_query_terms_match_the_documented_tokenizer(tmp_path):
    store, _, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    hits = index.search(context("reader"), "付款 日期")
    assert len(hits) == 1
    assert hits[0].entry.content == TEXT


def test_refresh_is_idempotent(tmp_path):
    store, _, index = build(tmp_path)
    publish(store)
    first = index.refresh(context("reader"))
    second = index.refresh(context("reader"))
    assert first == second
    assert len(index) == 1
    assert index.count(context("reader")) == 1
    with sqlite3.connect(tmp_path / "retrieval.db") as db:
        assert db.execute("SELECT count(*) FROM retrieval_entries").fetchone()[0] == 1
        assert db.execute("SELECT version FROM retrieval_schema").fetchall() == [(1,)]


def test_reopening_the_index_keeps_its_rows(tmp_path):
    store, validator, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    reopened = AuthorizedIndex(tmp_path / "retrieval.db", store=store, validate_sources=validator)
    assert len(reopened) == 1
    assert reopened.count(context("reader")) == 1
    assert len(reopened.search(context("reader"), "payment")) == 1
    with sqlite3.connect(tmp_path / "retrieval.db") as db:
        assert db.execute("SELECT count(*) FROM retrieval_schema").fetchone()[0] == 1


def test_refresh_is_scoped_and_checks_the_requested_resources(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, "case:1", "synthetic alpha one", key="a")
    publish(store, "case:2", "synthetic alpha two", key="b")
    reader = context("reader", resources=("case:1", "case:2"))
    entries = index.refresh(reader, resource_ids=("case:2",))
    assert [entry.resource_id for entry in entries] == ["case:2"]
    assert index.count(reader, resource_id="case:1") == 0
    assert index.refresh(reader, resource_ids=()) == ()
    with pytest.raises(RetrievalUnavailable, match="scope"):
        index.refresh(reader, resource_ids=("case:9",))
    with pytest.raises(ValueError, match="resource id"):
        index.refresh(reader, resource_ids=("r" * 201,))
    with pytest.raises(ValueError, match="too many"):
        index.refresh(reader, resource_ids=tuple(f"case:{n}" for n in range(501)))


def test_drop_removes_only_the_named_scoped_resource(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, "case:1", "synthetic alpha one", key="a")
    publish(store, "case:2", "synthetic alpha two", key="b")
    reader = context("reader", resources=("case:1", "case:2"))
    index.refresh(reader)
    assert index.count(reader, resource_id="case:1") == 1
    assert index.drop(reader, resource_id="case:1") == 1
    assert index.drop(reader, resource_id="case:1") == 0
    assert index.count(reader) == 1
    narrow = context("reader", resources=("case:1",))
    with pytest.raises(RetrievalUnavailable, match="scope"):
        index.drop(narrow, resource_id="case:2")
    with pytest.raises(RetrievalUnavailable, match="scope"):
        index.count(narrow, resource_id="case:2")
    with pytest.raises(ValueError, match="resource id"):
        index.drop(reader, resource_id="")
    with pytest.raises(ValueError, match="resource id"):
        index.count(reader, resource_id="")


def test_unsupported_schema_version_is_refused(tmp_path):
    store, validator, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    with sqlite3.connect(tmp_path / "retrieval.db") as db:
        db.execute("UPDATE retrieval_schema SET version=2")
    with pytest.raises(IndexConflict, match="schema"):
        AuthorizedIndex(tmp_path / "retrieval.db", store=store, validate_sources=validator)


def test_shared_directory_and_missing_validator_are_refused(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    with pytest.raises(ValueError, match="validator"):
        AuthorizedIndex(tmp_path / "retrieval.db", store=store, validate_sources=None)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(ScopeError, match="owner-private"):
        AuthorizedIndex(shared / "retrieval.db", store=store, validate_sources=Validator())


def test_credential_bearing_binding_is_refused(tmp_path):
    store, _, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    tainted = context("reader").model_copy(
        update={"actor": Actor(id="ghp_abcdefghijkl", roles=("reader",))}
    )
    with pytest.raises(RetrievalUnavailable):
        index.search(tainted, "payment")
    with pytest.raises(RetrievalUnavailable):
        index.refresh(tainted)


def test_query_bounds_and_empty_query_are_refused(tmp_path):
    store, _, index = build(tmp_path)
    publish(store)
    index.refresh(context("reader"))
    for bad in ("", "   ", None, 5):
        with pytest.raises(ValueError, match="empty"):
            index.search(context("reader"), bad)
    with pytest.raises(ValueError, match="maximum length"):
        index.search(context("reader"), "a" * (MAX_QUERY_CHARS + 1))
    # A query of punctuation alone has no term that could match any content.
    assert index.search(context("reader"), "!!! ???") == ()


def test_content_and_identity_bounds_are_enforced(tmp_path):
    store, _, index = build(tmp_path)
    record = publish(store)
    oversized = "a" * (MAX_CONTENT_CHARS + 1)
    with pytest.raises(ValueError, match="content"):
        index.upsert(entry_for(record, start=0, end=len(oversized), content=oversized))
    changes = [
        ({"resource_id": "r" * 201}, "resource id"),
        ({"application_id": ""}, "application"),
        ({"tenant_id": "t" * 129}, "tenant"),
        ({"locator": "l" * 201}, "locator"),
    ]
    for update, message in changes:
        with pytest.raises(ValueError, match=message):
            index.upsert(entry_for(record, **update))
    assert len(index) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"start": 5, "end": 3},
        {"start": 0, "end": 4},
        {"version": 0},
        {"digest": "A" * 64},
    ],
)
def test_entry_model_requires_a_consistent_span(changes):
    values = {
        "application_id": "echo",
        "tenant_id": "firm-a",
        "resource_id": "case:1",
        "version": 1,
        "digest": "a" * 64,
        "locator": "",
        "start": 0,
        "end": 3,
        "content": "abc",
    }
    values.update(changes)
    with pytest.raises(ValidationError):
        IndexEntry(**values)


def test_search_on_an_empty_index_returns_nothing(tmp_path):
    store, validator, index = build(tmp_path)
    publish(store)
    assert index.search(context("reader"), "payment") == ()
    assert index.count(context("reader")) == 0
    assert len(index) == 0
    assert validator.calls == []


def test_punctuation_only_content_is_never_matched(tmp_path):
    store, _, index = build(tmp_path)
    publish(store, text="!!! ???")
    index.refresh(context("reader"))
    assert len(index) == 1
    assert index.search(context("reader"), "alpha") == ()
    assert index.search(context("reader"), "!!!") == ()


def test_current_authorization_happens_before_scoring(tmp_path, monkeypatch):
    from forgeharness.sdk import retrieval

    store, validator, index = build(tmp_path, validator=Validator(allowed=False))
    publish(store)
    index.refresh(context())
    scored = []
    original = retrieval._score

    def observe(terms, entries):
        scored.extend(entries)
        return original(terms, entries)

    monkeypatch.setattr(retrieval, "_score", observe)
    assert index.search(context(), "payment") == ()
    assert not scored
    assert validator.calls

"""Business review, source integrity, freshness and namespace isolation."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_sdk import context

from forgeharness.sdk import (
    Actor,
    RecordConflict,
    RecordDependency,
    RecordDraft,
    RecordUnavailable,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    ScopeError,
    SourceAnchor,
)


def owner(role="editor", *, actor=None, **changes):
    return context(
        actor=Actor(id=actor or role, roles=(role,)),
        scope=ResourceScope(tenant_id="firm-a", resources=("case:1", "product:1")),
        **changes,
    )


def draft(text="合成案件: 付款日期为 2026-01-02。", **changes):
    return RecordDraft(
        resource_id="case:1",
        kind="evidence",
        content=text,
        sources=(
            SourceAnchor(source="synthetic-document", version="1", locator="physical-page:2"),
        ),
        **changes,
    )


def published(store, value=None):
    store.save(owner(), value or draft(), expected_version=0, request_id="submission-1")
    store.review(owner("reviewer"), "case:1", expected_version=1, approve=True)
    return store.publish(owner("publisher"), "case:1", expected_version=1)


def test_revision_review_publish_edit_and_stale_approval(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    first = published(store)
    assert store.published(owner("reader"), "case:1") == first
    second = store.save(
        owner(), draft("更新后的合成证据"), expected_version=1, request_id="submission-2"
    )
    assert second.version == 2 and second.reviewer is None
    assert store.read(owner("reader"), "case:1", version=1).state == "withdrawn"
    with pytest.raises(RecordUnavailable):
        store.published(owner("reader"), "case:1")
    with pytest.raises(RecordConflict, match="stale"):
        store.publish(owner("publisher"), "case:1", expected_version=1)
    with pytest.raises(RecordConflict, match="review"):
        store.publish(owner("publisher"), "case:1", expected_version=2)
    assert [e["operation"] for e in store.audit(owner("reader"), "case:1")] == [
        "draft.created",
        "approved",
        "published",
        "draft.created",
    ]


def test_retry_after_newer_edit_does_not_create_a_third_revision(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    first = store.save(owner(), draft(), expected_version=0, request_id="same-business-request")
    store.save(owner(), draft("edited"), expected_version=1, request_id="edit")
    retried = store.save(
        owner(run_id="new-run"), draft(), expected_version=0, request_id="same-business-request"
    )
    assert retried.version == first.version
    assert store.read(owner(), "case:1").version == 2
    with pytest.raises(RecordConflict, match="payload"):
        store.save(
            owner(), draft("changed"), expected_version=0, request_id="same-business-request"
        )


@pytest.mark.parametrize("change", ["tenant", "app", "resource", "role"])
def test_reads_filter_authorization_and_namespace_before_content(tmp_path, change):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    published(store)
    caller = owner("reader")
    if change == "tenant":
        caller = caller.model_copy(
            update={"scope": ResourceScope(tenant_id="firm-b", resources=("case:1",))}
        )
    elif change == "app":
        caller = caller.model_copy(update={"application_id": "other"})
    elif change == "resource":
        caller = caller.model_copy(
            update={"scope": ResourceScope(tenant_id="firm-a", resources=("case:2",))}
        )
    else:
        caller = owner("visitor")
    with pytest.raises((ScopeError, RecordUnavailable)):
        store.read(caller, "case:1")


def test_live_revocation_and_independent_review(tmp_path):
    class Policy(RoleRecordPolicy):
        revoked = False

        def authorize(self, *args):
            return not self.revoked and super().authorize(*args)

    policy = Policy()
    store = RevisionStore(tmp_path / "records.db", policy=policy)
    store.save(owner(), draft(), expected_version=0, request_id="r")
    with pytest.raises(ScopeError, match="independent"):
        store.review(owner("reviewer", actor="editor"), "case:1", expected_version=1, approve=True)
    policy.revoked = True
    with pytest.raises(ScopeError):
        store.review(owner("reviewer"), "case:1", expected_version=1, approve=True)
    policy.revoked = False
    store.review(owner("reviewer"), "case:1", expected_version=1, approve=False)
    with pytest.raises(RecordConflict):
        store.publish(owner("publisher"), "case:1", expected_version=1)


def test_expiry_checked_both_at_publication_and_read(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    store.save(
        owner(),
        draft(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        expected_version=0,
        request_id="r",
    )
    store.review(owner("reviewer"), "case:1", expected_version=1, approve=True)
    with pytest.raises(RecordUnavailable):
        store.publish(owner("publisher"), "case:1", expected_version=1)
    with pytest.raises(ValidationError):
        draft(expires_at=datetime.now().replace(tzinfo=None))


def test_cas_concurrent_edits_have_one_winner(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    published(store)

    def save(n):
        try:
            return store.save(
                owner(), draft(str(n)), expected_version=1, request_id=f"edit-{n}"
            ).version
        except RecordConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (1, 2)))
    assert sorted(map(str, results)) == ["2", "conflict"]


def test_withdraw_delete_and_retry_cannot_resurrect_content(tmp_path):
    path = tmp_path / "records.db"
    store = RevisionStore(path, policy=RoleRecordPolicy())
    published(store)
    store.withdraw(owner("publisher"), "case:1", expected_version=1)
    with pytest.raises(RecordUnavailable):
        store.published(owner("reader"), "case:1")
    store.delete(owner("admin"), "case:1")
    store.delete(owner("admin"), "case:1")
    assert store.audit(owner("admin"), "case:1") == ()
    with pytest.raises(RecordUnavailable):
        store.save(owner(), draft(), expected_version=0, request_id="submission-1")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM record_revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM record_requests").fetchone()[0] == 0


def test_credentials_transformed_before_storage_and_digest(tmp_path):
    path = tmp_path / "records.db"
    store = RevisionStore(path, policy=RoleRecordPolicy())
    value = store.save(
        owner(), draft("api_key=example-sensitive-value"), expected_version=0, request_id="r"
    )
    assert "example-sensitive" not in value.content
    assert store.read(owner(), "case:1").digest == value.digest
    with sqlite3.connect(path) as db:
        assert "example-sensitive" not in "\n".join(db.iterdump())
    with pytest.raises(ValueError):
        store.save(owner(), draft(), expected_version=1, request_id="Bearer abcdefghijkl")


@pytest.mark.parametrize("expected", [-1, True, "0"])
def test_invalid_revision_preconditions_rejected(tmp_path, expected):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    with pytest.raises(ValueError):
        store.save(owner(), draft(), expected_version=expected, request_id="r")


def test_future_schema_and_tampered_payload_rejected(tmp_path):
    path = tmp_path / "records.db"
    store = RevisionStore(path, policy=RoleRecordPolicy())
    published(store)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE record_revisions SET payload=replace(payload,'付款','退款')")
    with pytest.raises(RecordConflict, match="integrity"):
        store.read(owner(), "case:1")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE record_schema SET version=9")
    with pytest.raises(RecordConflict, match="schema"):
        RevisionStore(path, policy=RoleRecordPolicy())


def test_store_rejects_shared_directory(tmp_path: Path):
    tmp_path.chmod(0o755)
    with pytest.raises(ScopeError):
        RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())


def test_dependency_versions_and_review_preconditions(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    source = published(store)
    dependency = RecordDependency(resource_id=source.resource_id, version=1, digest=source.digest)
    derived = RecordDraft(
        resource_id="product:1", kind="draft", content="derived", dependencies=(dependency,)
    )
    store.save(owner(), derived, expected_version=0, request_id="derived")
    store.save(owner(), draft("changed"), expected_version=1, request_id="edit")
    store.review(owner("reviewer"), "case:1", expected_version=2, approve=True)
    store.publish(owner("publisher"), "case:1", expected_version=2)
    with pytest.raises(RecordUnavailable, match="superseded"):
        store.review(owner("reviewer"), "product:1", expected_version=1, approve=True)
    # Rejection remains possible even when source evidence has expired/changed.
    assert (
        store.review(owner("reviewer"), "product:1", expected_version=1, approve=False).state
        == "rejected"
    )
    with pytest.raises(RecordConflict, match="draft"):
        store.review(owner("reviewer"), "case:1", expected_version=2, approve=True)
    with pytest.raises(ValueError, match="boolean"):
        store.review(owner("reviewer"), "case:1", expected_version=2, approve="true")
    with pytest.raises(ValueError, match="positive"):
        store.read(owner(), "case:1", version=True)


def test_cycle_rejected_and_shared_dependency_checked_once(tmp_path, monkeypatch):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    source = published(store)
    dependency = RecordDependency(resource_id=source.resource_id, version=1, digest=source.digest)
    with pytest.raises(RecordConflict, match="cyclic"):
        store.save(
            owner(), draft(dependencies=(dependency,)), expected_version=1, request_id="cycle"
        )
    calls = []
    original = store._revision

    def counted(db, key, version):
        calls.append(key)
        return original(db, key, version)

    monkeypatch.setattr(store, "_revision", counted)
    store.save(
        owner(),
        RecordDraft(
            resource_id="product:1",
            kind="draft",
            content="derived",
            dependencies=(dependency,) * 50,
        ),
        expected_version=0,
        request_id="derived",
    )
    assert calls.count(("echo", "firm-a", "case:1")) == 1


def test_dependency_expansion_has_a_total_bound(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    scope = ResourceScope(tenant_id="firm-a", resources=tuple(f"r{i}" for i in range(260)))
    ctx = owner().model_copy(update={"scope": scope})
    reviewer = ctx.model_copy(update={"actor": Actor(id="reviewer", roles=("reviewer",))})
    publisher = ctx.model_copy(update={"actor": Actor(id="publisher", roles=("publisher",))})
    refs = []
    groups = []
    for index in range(259):
        dependencies = () if index < 256 else tuple(refs[(index - 256) * 100 : (index - 255) * 100])
        value = store.save(
            ctx,
            RecordDraft(
                resource_id=f"r{index}", kind="fixture", content="x", dependencies=dependencies
            ),
            expected_version=0,
            request_id=f"r{index}",
        )
        store.review(reviewer, value.resource_id, expected_version=1, approve=True)
        store.publish(publisher, value.resource_id, expected_version=1)
        ref = RecordDependency(resource_id=value.resource_id, version=1, digest=value.digest)
        if index < 256:
            refs.append(ref)
        else:
            groups.append(ref)
    with pytest.raises(RecordConflict, match="256"):
        store.save(
            ctx,
            RecordDraft(
                resource_id="r259", kind="fixture", content="x", dependencies=tuple(groups)
            ),
            expected_version=0,
            request_id="large",
        )

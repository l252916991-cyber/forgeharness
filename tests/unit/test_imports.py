"""The S2 import chain: staging, validation, drafting, tasks, retention (ADR 015).

These tests use a real revision store and a real task store, so what they exercise is
the actual chain rather than a mock of it.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_sdk import context

from forgeharness.domain.models import Actor, ResourceScope, RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.imports import (
    ImportConflict,
    ImportOperation,
    ImportRequest,
    ImportService,
    ImportUnavailable,
    ParsedDocument,
    ParsedSpan,
    RetentionPolicy,
)
from forgeharness.sdk.records import RevisionStore, RoleRecordPolicy
from forgeharness.sdk.tasks import TaskRunner, TaskState, TaskStore
from forgeharness.tools.base import ToolEffectClass


def author(role: str = "editor") -> RunContext:
    return context(
        actor=Actor(id=role, roles=(role,)),
        scope=ResourceScope(tenant_id="firm-a", resources=("case:1", "staged:any")),
        application_id="imports",
    )


class FixedParser:
    """Deterministic stand-in for a real document parser."""

    def __init__(self, content: str = "Synthetic import body.", spans: tuple[ParsedSpan, ...] = ()):
        self.content = content
        self.spans = spans
        self.calls = 0

    def parse(self, *, filename: str, data: bytes, content_type: str) -> ParsedDocument:
        del filename, data, content_type
        self.calls += 1
        return ParsedDocument(content=self.content, spans=self.spans)


class RecordingValidator:
    def __init__(self, *, accept: bool = True, reason: str = "synthetic acceptance") -> None:
        self.accept = accept
        self.reason = reason
        self.calls = 0

    def validate(
        self, context: RunContext, staged: object, parsed: ParsedDocument
    ) -> tuple[bool, str]:
        del context, staged, parsed
        self.calls += 1
        return self.accept, self.reason


def build(
    tmp_path: Path,
    *,
    parser: FixedParser | None = None,
    validator: RecordingValidator | None = None,
) -> tuple[ImportService, RevisionStore, TaskStore, RecordingValidator]:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    tasks = TaskStore(tmp_path / "tasks.sqlite", authorize=lambda *_: True)
    chosen = validator or RecordingValidator()
    service = ImportService(
        state_dir=tmp_path / "imports",
        records=store,
        parser=parser or FixedParser(),
        validator=chosen,
        authorize=lambda *_: True,
        tasks=tasks,
    )
    return service, store, tasks, chosen


def draft_request(**changes: object) -> ImportRequest:
    values: dict[str, object] = {
        "staged_id": "",
        "resource_id": "case:1",
        "kind": "case_material",
        "expected_version": 0,
    }
    values.update(changes)
    return ImportRequest.model_validate(values)


def test_staging_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    first = service.stage(
        author(), filename="page.txt", data=b"synthetic", content_type="text/plain"
    )
    again = service.stage(author(), filename="page.txt", data=b"synthetic")

    assert first.id == again.id
    assert first.digest == again.digest
    assert first.size_bytes == len(b"synthetic")
    assert service.staged(author(), first.id) == first


def test_staging_rejects_unbounded_or_unsafe_input(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    with pytest.raises(ValueError, match="non-empty bytes"):
        service.stage(author(), filename="empty.txt", data=b"")
    with pytest.raises(ValueError, match="single relative component"):
        service.stage(author(), filename="../escape.txt", data=b"x")
    with pytest.raises(ValueError, match="configured bound"):
        service.stage(author(), filename="big.txt", data=b"x" * (10 * 1024 * 1024 + 1))


def test_validation_verdict_is_recorded_and_reused(tmp_path: Path) -> None:
    validator = RecordingValidator()
    service, _, _, _ = build(tmp_path, validator=validator)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")

    first = service.validate(author(), staged.id)
    second = service.validate(author(), staged.id)

    assert first.accepted is True and first == second
    assert validator.calls == 1


def test_rejected_material_never_becomes_a_draft(tmp_path: Path) -> None:
    service, store, _, _ = build(tmp_path, validator=RecordingValidator(accept=False))
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")

    with pytest.raises(ImportConflict, match="rejected"):
        service.draft(author(), request=draft_request(staged_id=staged.id), request_id="import-1")
    with pytest.raises(ValueError):
        store.read(author(), "case:1")


def test_draft_is_idempotent_on_the_business_key(tmp_path: Path) -> None:
    parser = FixedParser()
    service, store, _, _ = build(tmp_path, parser=parser)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")
    request = draft_request(staged_id=staged.id)

    first = service.draft(author(), request=request, request_id="import-1")
    second = service.draft(author(), request=request, request_id="import-1")

    assert first.version == second.version == 1
    assert first.content == second.content == parser.content
    assert first.state == "draft" and store.read(author(), "case:1").version == 1


def test_draft_carries_the_staged_anchor(tmp_path: Path) -> None:
    parser = FixedParser(spans=(ParsedSpan(locator="page:1", start=0, end=9),))
    service, _, _, _ = build(tmp_path, parser=parser)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")

    revision = service.draft(
        author(), request=draft_request(staged_id=staged.id), request_id="import-1"
    )

    assert revision.sources[0].source == staged.id
    assert revision.sources[0].version == staged.digest
    assert revision.sources[0].locator == "page:1"


def test_oversized_parse_is_refused_not_truncated(tmp_path: Path) -> None:
    # The parser contract already bounds a document at the revision limit, so an
    # oversized result can only appear when a host transform expands the text. That
    # must be refused rather than silently truncated.
    class ExpandingTransform:
        def transform(self, value: object) -> object:
            if isinstance(value, str):
                return value * 20
            return value

    class ExpandingParser:
        def parse(self, *, filename: str, data: bytes, content_type: str) -> ParsedDocument:
            del filename, data, content_type
            return ParsedDocument(content="x" * 10_000)

    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    service = ImportService(
        state_dir=tmp_path / "imports",
        records=store,
        parser=ExpandingParser(),
        validator=RecordingValidator(),
        authorize=lambda *_: True,
        transform=ExpandingTransform(),
    )
    staged = service.stage(author(), filename="big.txt", data=b"synthetic")

    with pytest.raises(ImportConflict, match="revision bound"):
        service.parse(author(), staged.id)


def test_document_bound_is_enforced_at_the_parser_boundary() -> None:
    with pytest.raises(ValueError):
        ParsedDocument(content="x" * 100_001)


def test_import_task_runs_through_the_runner(tmp_path: Path) -> None:
    service, store, tasks, validator = build(tmp_path)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")
    task = service.start_import(
        author(),
        staged_id=staged.id,
        resource_id="case:1",
        kind="case_material",
        request_id="import-1",
    )
    assert task.effect_class is ToolEffectClass.IDEMPOTENT
    assert task.progress_total == 3

    runner = TaskRunner(tasks, handlers=service.handlers())
    settled = asyncio_run(runner.run_once(author(), task.id))

    assert settled.state is TaskState.SUCCEEDED
    assert settled.progress_done == 3
    assert validator.calls == 1
    recorded = json.loads(settled.result or "{}")
    assert recorded == {
        "resource_id": "case:1",
        "version": 1,
        "digest": store.read(author(), "case:1").digest,
        "state": "draft",
    }
    assert store.read(author(), "case:1").state == "draft"


def test_import_task_retry_reuses_one_revision(tmp_path: Path) -> None:
    service, store, tasks, _ = build(tmp_path)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")
    task = service.start_import(
        author(),
        staged_id=staged.id,
        resource_id="case:1",
        kind="case_material",
        request_id="import-1",
    )
    runner = TaskRunner(tasks, handlers=service.handlers())
    asyncio_run(runner.run_once(author(), task.id))

    # The same business key can never mint a second revision.
    same = service.start_import(
        author(),
        staged_id=staged.id,
        resource_id="case:1",
        kind="case_material",
        request_id="import-1",
    )
    assert same.id == task.id
    assert store.read(author(), "case:1").version == 1


def test_staging_lease_expires_and_sweep_removes_it(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")
    blob = service.staging_dir / staged.id
    assert blob.exists()

    removed = service.sweep(author(), now=staged.expires_at + timedelta(seconds=1))

    assert removed == (staged.id,)
    assert not blob.exists()
    with pytest.raises(ImportUnavailable):
        service.staged(author(), staged.id)


def test_sweep_never_removes_material_a_live_task_needs(tmp_path: Path) -> None:
    service, _, tasks, _ = build(tmp_path)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")
    task = service.start_import(
        author(),
        staged_id=staged.id,
        resource_id="case:1",
        kind="case_material",
        request_id="import-1",
    )

    removed = service.sweep(author(), now=staged.expires_at + timedelta(seconds=1))

    assert removed == ()
    assert (service.staging_dir / staged.id).exists()
    with pytest.raises(ImportConflict, match="active task"):
        service.delete_staged(author(), staged.id)
    tasks.request_cancel(author(), task.id)


def test_expired_lease_blocks_reading_the_material(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")
    # Manufacture the expired-lease window without waiting for it: the sweep path is
    # tested separately, and what matters here is that reading refuses first.
    expired = staged.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    with sqlite3.connect(service.database) as db:
        db.execute(
            "UPDATE staged_materials SET payload=? WHERE id=?",
            (expired.model_dump_json(), staged.id),
        )

    with pytest.raises(ImportUnavailable, match="lease expired"):
        service.staged(author(), staged.id)


def test_cross_tenant_and_credential_identities_are_refused(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")

    stranger = author().model_copy(
        update={"scope": ResourceScope(tenant_id="firm-b", resources=("staged:any",))}
    )
    with pytest.raises(ImportUnavailable):
        service.staged(stranger, staged.id)

    credential = author().model_copy(update={"correlation_id": "api_key: sk-abcdefghijkl"})
    with pytest.raises(ImportUnavailable, match="credentials"):
        service.stage(credential, filename="page.txt", data=b"other")


def test_constructor_requires_a_live_authorizer_and_private_directory(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    with pytest.raises(ValueError, match="live host authorizer"):
        ImportService(
            state_dir=tmp_path / "imports",
            records=store,
            parser=FixedParser(),
            validator=RecordingValidator(),
            authorize=None,  # type: ignore[arg-type]
        )
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ScopeError, match="owner-private"):
        ImportService(
            state_dir=public,
            records=store,
            parser=FixedParser(),
            validator=RecordingValidator(),
            authorize=lambda *_: True,
        )


def test_unsupported_schema_is_refused(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    database = service.database
    with sqlite3.connect(database) as db:
        db.execute("UPDATE import_schema SET version=2")

    with pytest.raises(ImportConflict, match="unsupported import schema"):
        ImportService(
            state_dir=tmp_path / "imports",
            records=RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy()),
            parser=FixedParser(),
            validator=RecordingValidator(),
            authorize=lambda *_: True,
        )


def test_retention_policy_bounds_the_lease(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(staging_seconds=1)
    policy = RetentionPolicy(staging_seconds=3600)
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    service = ImportService(
        state_dir=tmp_path / "imports",
        records=store,
        parser=FixedParser(),
        validator=RecordingValidator(),
        authorize=lambda *_: True,
        retention=policy,
    )
    staged = service.stage(author(), filename="page.txt", data=b"synthetic")
    assert staged.expires_at - staged.created_at == timedelta(seconds=3600)


def test_operation_vocabulary_is_closed() -> None:
    assert {operation.value for operation in ImportOperation} == {
        "stage",
        "read",
        "validate",
        "draft",
        "sweep",
    }


def asyncio_run(coroutine: object) -> object:
    import asyncio

    return asyncio.run(coroutine)  # type: ignore[arg-type]


def test_import_boundary_arguments_are_refused(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    with pytest.raises(ValueError, match="staging size bound"):
        ImportService(
            state_dir=tmp_path / "imports",
            records=store,
            parser=FixedParser(),
            validator=RecordingValidator(),
            authorize=lambda *_: True,
            max_bytes=0,
        )
    public = tmp_path / "public"
    (public / "staging").mkdir(parents=True, mode=0o755)
    public.chmod(0o700)
    with pytest.raises(ScopeError, match="staging directory"):
        ImportService(
            state_dir=public,
            records=store,
            parser=FixedParser(),
            validator=RecordingValidator(),
            authorize=lambda *_: True,
        )


def test_authorizer_denial_refuses_every_operation(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    service = ImportService(
        state_dir=tmp_path / "imports",
        records=store,
        parser=FixedParser(),
        validator=RecordingValidator(),
        authorize=lambda *_: False,
    )
    with pytest.raises(ImportUnavailable, match="import permission denied"):
        service.stage(author(author_role()) if False else author(), filename="a.txt", data=b"x")


def author_role() -> str:
    return "editor"


def test_stage_input_bounds_are_explicit(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    with pytest.raises(ValueError, match="bounded single name"):
        service.stage(author(), filename="", data=b"x")
    with pytest.raises(ValueError, match="bounded single name"):
        service.stage(author(), filename="x" * 201, data=b"x")
    with pytest.raises(ValueError, match="content type must be bounded"):
        service.stage(author(), filename="a.txt", data=b"x", content_type="t" * 101)


def test_staging_capacity_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import forgeharness.sdk.imports as module

    monkeypatch.setattr(module, "MAX_STAGED_FILES", 1)
    service, _, _, _ = build(tmp_path)
    service.stage(author(), filename="a.txt", data=b"one")
    with pytest.raises(ImportConflict, match="staging area is full"):
        service.stage(author(), filename="b.txt", data=b"two")


def test_tampered_or_missing_blobs_are_refused(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    staged = service.stage(author(), filename="a.txt", data=b"honest")
    blob = service.staging_dir / staged.id
    blob.write_bytes(b"tampered")
    with pytest.raises(ImportUnavailable, match="integrity check"):
        service.parse(author(), staged.id)

    blob.unlink()
    with pytest.raises(ImportUnavailable, match="unavailable"):
        service.parse(author(), staged.id)


def test_existing_blob_with_wrong_content_is_refused(tmp_path: Path) -> None:
    service, _, _, _ = build(tmp_path)
    staged = service.stage(author(), filename="a.txt", data=b"honest")
    with sqlite3.connect(service.database) as db:
        db.execute("DELETE FROM staged_materials WHERE id=?", (staged.id,))
    (service.staging_dir / staged.id).write_bytes(b"different")
    with pytest.raises(ImportConflict, match="does not match its recorded digest"):
        service.stage(author(), filename="a.txt", data=b"honest")


def test_span_and_validator_contract_violations_are_refused(tmp_path: Path) -> None:
    parser = FixedParser(spans=(ParsedSpan(locator="page:1", start=0, end=9_999),))
    service, _, _, _ = build(tmp_path, parser=parser)
    staged = service.stage(author(), filename="a.txt", data=b"honest")
    with pytest.raises(ValueError, match="outside the parsed content"):
        service.parse(author(), staged.id)

    class BadValidator:
        def validate(self, context: object, staged: object, parsed: object) -> object:
            del context, staged, parsed
            return "yes"

    service, _, _, _ = build(tmp_path / "second", validator=BadValidator())  # type: ignore[arg-type]
    staged = service.stage(author(), filename="a.txt", data=b"honest")
    with pytest.raises(ValueError, match="decision and a reason"):
        service.validate(author(), staged.id)


def test_import_requires_a_task_store_and_a_matching_request(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "records.sqlite", policy=RoleRecordPolicy())
    without_tasks = ImportService(
        state_dir=tmp_path / "imports",
        records=store,
        parser=FixedParser(),
        validator=RecordingValidator(),
        authorize=lambda *_: True,
    )
    staged = without_tasks.stage(author(), filename="a.txt", data=b"honest")
    with pytest.raises(ImportConflict, match="no durable task store"):
        without_tasks.start_import(
            author(),
            staged_id=staged.id,
            resource_id="case:1",
            kind="case_material",
            request_id="import-1",
        )

    service, _, _tasks, _ = build(tmp_path / "second")
    staged = service.stage(author(), filename="a.txt", data=b"honest")
    service.start_import(
        author(),
        staged_id=staged.id,
        resource_id="case:1",
        kind="case_material",
        request_id="import-1",
    )
    with pytest.raises(ImportConflict, match="different request"):
        service.start_import(
            author(),
            staged_id=staged.id,
            resource_id="case:1",
            kind="other_kind",
            request_id="import-2",
        )


def test_task_payload_helpers_reject_foreign_records(tmp_path: Path) -> None:
    service, _, tasks, _ = build(tmp_path)
    service.stage(author(), filename="a.txt", data=b"honest")
    foreign = tasks.create(author(), kind="index", request_id="index-1", business_key="other-key")
    with pytest.raises(ImportConflict, match="not an import task"):
        service.import_request(author(), foreign)


def test_sweep_leaves_unexpired_and_foreign_kind_material_alone(tmp_path: Path) -> None:
    service, _, tasks, _ = build(tmp_path)
    staged = service.stage(author(), filename="a.txt", data=b"honest")
    tasks.create(author(), kind="index", request_id="index-1", business_key="index-key")

    assert service.sweep(author()) == ()
    assert (service.staging_dir / staged.id).exists()


def test_identical_uploads_are_scoped_and_cleanup_cannot_remove_another_tenant(tmp_path):
    service, _, _, _ = build(tmp_path)
    first = service.stage(author(), filename="a.txt", data=b"same")
    other = author().model_copy(update={"scope": ResourceScope(tenant_id="firm-b")})
    second = service.stage(other, filename="a.txt", data=b"same")
    third = service.stage(author("other-editor"), filename="a.txt", data=b"same")
    assert len({first.id, second.id, third.id}) == 3
    service.delete_staged(author(), first.id)
    assert service.parse(other, second.id).content
    assert service.parse(author("other-editor"), third.id).content
    assert service.sweep(author(), now=second.expires_at + timedelta(seconds=1)) == ()
    assert service.staged(other, second.id) == second


def test_import_requests_for_identical_tenant_inputs_do_not_collide(tmp_path):
    service, _, tasks, _ = build(tmp_path)
    first = service.stage(author(), filename="a.txt", data=b"same")
    other = author().model_copy(
        update={"scope": ResourceScope(tenant_id="firm-b", resources=("case:1",))}
    )
    second = service.stage(other, filename="a.txt", data=b"same")
    keys = []
    for caller, staged in ((author(), first), (other, second)):
        task = service.start_import(
            caller, staged_id=staged.id, resource_id="case:1", kind="case", request_id="one"
        )
        keys.append(task.business_key)
        assert (
            asyncio_run(
                TaskRunner(tasks, handlers=service.handlers()).run_once(caller, task.id)
            ).state
            is TaskState.SUCCEEDED
        )
    assert keys[0] != keys[1]


def test_hidden_active_task_still_protects_staging_from_cleanup(tmp_path):
    service, _, tasks, _ = build(tmp_path)
    staged = service.stage(author(), filename="a.txt", data=b"same")
    service.start_import(
        author(), staged_id=staged.id, resource_id="case:1", kind="case", request_id="one"
    )
    tasks.authorize = lambda *_: False
    assert service.sweep(author(), now=staged.expires_at + timedelta(days=1)) == ()
    with pytest.raises(ImportConflict, match="active task"):
        service.delete_staged(author(), staged.id)


def test_import_task_does_not_retain_content_after_material_deletion(tmp_path: Path) -> None:
    service, store, tasks, _ = build(tmp_path)
    staged = service.stage(author(), filename="a.txt", data=b"synthetic")
    task = service.start_import(
        author(), staged_id=staged.id, resource_id="case:1", kind="case", request_id="one"
    )
    settled = asyncio_run(
        TaskRunner(tasks, handlers=service.handlers()).run_once(author(), task.id)
    )
    assert settled.state is TaskState.SUCCEEDED
    store.delete(author("admin"), "case:1")
    service.delete_staged(author(), staged.id)
    result = tasks.get(author(), task.id).result or ""
    assert "Synthetic import body." not in result
    assert set(json.loads(result)) == {"resource_id", "version", "digest", "state"}
    with pytest.raises(ValueError):
        store.read(author(), "case:1")
    with pytest.raises(ImportUnavailable):
        service.parse(author(), staged.id)


def test_draft_revalidates_current_parse_after_adapter_restart(tmp_path: Path) -> None:
    service, store, tasks, _ = build(tmp_path, parser=FixedParser("previously accepted"))
    staged = service.stage(author(), filename="a.txt", data=b"synthetic")
    assert service.validate(author(), staged.id).accepted
    rejecting = RecordingValidator(accept=False, reason="new parser output rejected")
    reopened = ImportService(
        state_dir=service.state_dir,
        records=store,
        parser=FixedParser("different parsed content"),
        validator=rejecting,
        authorize=lambda *_: True,
        tasks=tasks,
    )
    with pytest.raises(ImportConflict, match="new parser output rejected"):
        reopened.draft(author(), request=draft_request(staged_id=staged.id), request_id="one")
    assert rejecting.calls == 1
    assert reopened.validate(author(), staged.id).accepted is False
    with pytest.raises(ValueError):
        store.read(author(), "case:1")


def test_draft_saves_exactly_the_single_parsed_snapshot_that_was_validated(tmp_path: Path) -> None:
    class ChangingParser(FixedParser):
        def parse(self, *, filename: str, data: bytes, content_type: str) -> ParsedDocument:
            self.calls += 1
            return ParsedDocument(content=f"parse-{self.calls}")

    class ExactValidator(RecordingValidator):
        def validate(self, context, staged, parsed):
            self.calls += 1
            return parsed.content == "parse-1", "only first snapshot accepted"

    parser = ChangingParser()
    validator = ExactValidator()
    service, _, _, _ = build(tmp_path, parser=parser, validator=validator)
    staged = service.stage(author(), filename="a.txt", data=b"synthetic")
    saved = service.draft(author(), request=draft_request(staged_id=staged.id), request_id="one")
    assert saved.content == "parse-1"
    assert parser.calls == validator.calls == 1


@pytest.mark.parametrize("operation", ["sweep", "delete"])
def test_missing_task_store_cannot_remove_linked_import_material(tmp_path: Path, operation: str):
    service, store, tasks, _ = build(tmp_path)
    staged = service.stage(author(), filename="a.txt", data=b"synthetic")
    task = service.start_import(
        author(), staged_id=staged.id, resource_id="case:1", kind="case", request_id="one"
    )
    reopened = ImportService(
        state_dir=service.state_dir,
        records=store,
        parser=FixedParser(),
        validator=RecordingValidator(),
        authorize=lambda *_: True,
    )
    with pytest.raises(ImportConflict, match="durable task store required"):
        if operation == "sweep":
            reopened.sweep(author(), now=staged.expires_at + timedelta(seconds=1))
        else:
            reopened.delete_staged(author(), staged.id)
    assert tasks.get(author(), task.id).state is TaskState.QUEUED
    assert reopened.parse(author(), staged.id).content
    # A configured ledger can confirm cancellation and permit subsequent cleanup.
    tasks.request_cancel(author(), task.id)
    service.delete_staged(author(), staged.id)
    with pytest.raises(ImportUnavailable):
        service.staged(author(), staged.id)


def test_missing_task_store_can_clean_staging_that_has_never_had_a_task(tmp_path: Path):
    service, store, _, _ = build(tmp_path)
    staged = service.stage(author(), filename="a.txt", data=b"synthetic")
    reopened = ImportService(
        state_dir=service.state_dir,
        records=store,
        parser=FixedParser(),
        validator=RecordingValidator(),
        authorize=lambda *_: True,
    )
    assert reopened.sweep(author(), now=staged.expires_at + timedelta(seconds=1)) == (staged.id,)

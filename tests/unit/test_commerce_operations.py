import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from examples.commerce_demo import build, web_app
from forgeharness.applications.commerce.operations import (
    BackupError,
    bind_application_state,
    create_backup,
    restore_backup,
    service_lock,
    verify_backup,
)


def test_new_workbench_cannot_adopt_legacy_state_or_another_application(tmp_path: Path) -> None:
    state = tmp_path / "state"
    build(state)
    (state / "application-binding.json").unlink()
    with service_lock(state), pytest.raises(BackupError, match="legacy"):
        bind_application_state(state, application_id="commerce-workbench", application_version="1")
    fresh = tmp_path / "fresh"
    fresh.mkdir(mode=0o700)
    with service_lock(fresh):
        bind_application_state(fresh, application_id="commerce-workbench", application_version="1")
        with pytest.raises(BackupError, match="binding differs"):
            bind_application_state(
                fresh, application_id="commerce-consultation", application_version="2"
            )
    backup = tmp_path / "backup"
    manifest = create_backup(fresh, backup)
    assert manifest.application_id == "commerce-workbench"
    assert manifest.application_version == "1"
    (backup / "backup-manifest.json").write_text(
        manifest.model_copy(update={"application_id": "commerce-consultation"}).model_dump_json()
    )
    with pytest.raises(BackupError, match="binding differs"):
        verify_backup(backup)


def test_backup_restore_preserves_all_local_state_and_rejects_relocation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    store, _, sessions = build(state)
    assert store.product("shop-a", "SYN-NB-001")["version"] == 3
    assert sessions.database.is_file()

    backup = tmp_path / "backup"
    manifest = create_backup(state, backup)
    assert manifest.files
    assert verify_backup(backup) == manifest
    with pytest.raises(BackupError, match="original absolute"):
        restore_backup(backup, tmp_path / "different-state")

    shutil.rmtree(state)
    restored = restore_backup(backup, state)
    assert restored == manifest
    rebuilt_store, _, rebuilt_sessions = build(state)
    assert rebuilt_store.product("shop-a", "SYN-NB-001")["version"] == 3
    assert rebuilt_sessions.database.is_file()


def test_backup_refuses_running_service_and_detects_changed_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    app = web_app(state)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        with pytest.raises(BackupError, match="active"):
            create_backup(state, tmp_path / "running-backup")
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        with pytest.raises(BackupError, match="active"):
            create_backup(state, tmp_path / "restarted-backup")

    backup = tmp_path / "backup"
    manifest = create_backup(state, backup)
    relative = next(iter(manifest.files))
    (backup / relative).write_bytes(b"changed")
    with pytest.raises(BackupError, match="digest changed"):
        verify_backup(backup)


def test_service_initialization_refuses_an_active_state_operation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    build(state)
    with service_lock(state), pytest.raises(BackupError, match="active"):
        web_app(state)


def test_backup_refuses_symlinks_and_existing_destination(tmp_path: Path) -> None:
    state = tmp_path / "state"
    build(state)
    (state / "link").symlink_to(state / "commerce.sqlite")
    with pytest.raises(BackupError, match="symbolic links"):
        create_backup(state, tmp_path / "backup")
    (state / "link").unlink()
    destination = tmp_path / "backup"
    destination.mkdir(mode=0o700)
    with pytest.raises(BackupError, match="already exists"):
        create_backup(state, destination)


def test_pending_general_handoff_survives_backup_restore_and_resumes_once(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    customer = {"Authorization": "Bearer customer", "X-Session-Id": "restore-session"}
    supervisor = {
        "Authorization": "Bearer supervisor",
        "X-Session-Id": "restore-session",
    }
    with TestClient(web_app(state)) as client:
        submitted = client.post(
            "/commerce/consult",
            headers=customer,
            json={
                "request_id": "restore-handoff",
                "query": "这个问题没有对应商品, 请人工处理",
                "handoff": True,
            },
        )
        assert submitted.status_code == 200, submitted.text
        turn = submitted.json()
        assert turn["status"] == "awaiting_approval"

    backup = tmp_path / "backup"
    create_backup(state, backup)
    shutil.rmtree(state)
    restore_backup(backup, state)

    with TestClient(web_app(state)) as client:
        base = f"/harness/sessions/restore-session/turns/{turn['id']}"
        approved = client.post(base + "/approve", headers=supervisor, json={})
        assert approved.status_code == 200, approved.text
        resumed = client.post(
            base + "/resume",
            headers=supervisor,
            json={"grant": approved.json()},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "succeeded"

        tickets = client.get("/commerce/tickets", headers={"Authorization": "Bearer support"})
        assert tickets.status_code == 200, tickets.text
        rows = tickets.json()
        assert len(rows) == 1
        assert rows[0]["sku"] == ""
        assert rows[0]["id"] == resumed.json()["output"]

        replay = client.post(
            base + "/resume",
            headers=supervisor,
            json={"grant": approved.json()},
        )
        assert replay.status_code == 409, replay.text
        assert (
            len(client.get("/commerce/tickets", headers={"Authorization": "Bearer support"}).json())
            == 1
        )

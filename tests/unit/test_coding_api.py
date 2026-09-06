"""Actual CodingAgent approval lifecycle through the API-owned handler."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeharness.coding.api import APICodingHandler, _agent_response
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.knowledge.storage import SQLiteApplicationStore
from forgeharness.models.scripted import ScriptedModel


def _handler(path: Path, store: SQLiteApplicationStore) -> APICodingHandler:
    return APICodingHandler(
        data_dir=path,
        bindings=store,
        base_url="http://127.0.0.1:1/v1",
        model_name="scripted",
        api_key=None,
        timeout_seconds=1,
    )


async def test_coding_handler_approvals_are_exact_and_not_restored_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    store = SQLiteApplicationStore(tmp_path / "app.sqlite3", tmp_path / "media")
    store.bind_workspace("session", repository)

    def provider(*args: object, **kwargs: object) -> ScriptedModel:
        return ScriptedModel(
            [
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(
                            id="write1",
                            name="write_file",
                            arguments={"path": "first.txt", "content": "first"},
                        )
                    )
                ),
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(
                            id="write2",
                            name="write_file",
                            arguments={"path": "second.txt", "content": "second"},
                        )
                    )
                ),
                ModelResult(action=FinalAction(content="writes finished")),
            ]
        )

    monkeypatch.setattr("forgeharness.coding.api.OpenAICompatibleModel", provider)
    handler = _handler(tmp_path, store)
    absent = await handler.handle(session_id="absent", task="write")
    assert "没有绑定" in absent.answer
    response = await handler.handle(session_id="session", task="create two files")
    assert not (repository / "first.txt").exists()
    restarted = _handler(tmp_path, store)
    with pytest.raises(RuntimeError, match="lost after process restart"):
        await restarted.approve(response.run_id, granted_by="tester")
    second = await handler.approve(response.run_id, granted_by="tester")
    assert second.status == RunStatus.AWAITING_APPROVAL
    assert (repository / "first.txt").read_text() == "first"
    assert not (repository / "second.txt").exists()
    finished = await handler.approve(response.run_id, granted_by="tester")
    assert finished.status == RunStatus.SUCCEEDED
    assert (repository / "second.txt").read_text() == "second"
    assert response.run_id not in handler._active
    with pytest.raises(ValueError, match="no pending approval"):
        await handler.approve(response.run_id, granted_by="tester")
    with pytest.raises(KeyError, match="not found"):
        await handler.approve("missing", granted_by="tester")
    # A suspended run still owns a client, and shutdown must close it.
    other = await handler.handle(session_id="session", task="suspend")
    active_client = handler._active[other.run_id].client
    await handler.close()
    assert active_client.is_closed and not handler._active
    await restarted.close()


async def test_coding_handler_closes_clients_on_completion_and_start_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteApplicationStore(tmp_path / "app.sqlite3", tmp_path / "media")
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    store.bind_workspace("valid", repository)
    store.bind_workspace("invalid", tmp_path)
    monkeypatch.setattr(
        "forgeharness.coding.api.OpenAICompatibleModel",
        lambda *a, **k: ScriptedModel([ModelResult(action=FinalAction(content="done"))]),
    )
    handler = _handler(tmp_path, store)
    done = await handler.handle(session_id="valid", task="finish")
    assert done.answer == "done" and not handler._active
    with pytest.raises(ValueError, match="Git repository"):
        await handler.handle(session_id="invalid", task="finish")
    await handler.close()


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (RunStatus.FAILED, "model failed", "代码任务失败"),
        (RunStatus.EXHAUSTED, None, "代码任务状态"),
    ],
)
def test_coding_response_preserves_terminal_failure(
    status: RunStatus, error: str | None, expected: str
) -> None:
    result = RunResult(task_id="run", status=status, error=error, messages=(), usage=Usage())
    assert expected in _agent_response(result, "test").answer

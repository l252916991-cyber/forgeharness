"""End-to-end attachment identity and durable RAG evidence regressions."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from forgeharness.api import create_app
from forgeharness.knowledge.application import build_knowledge_application
from forgeharness.knowledge.indexes import SQLiteLexicalIndex
from forgeharness.knowledge.runs import KnowledgeRunStore
from forgeharness.observability.hash_chain import verify_trace


async def test_api_attachments_are_resolved_and_knowledge_runs_survive_restart(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        session = (await client.post("/v1/sessions")).json()["id"]
        upload = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "attachment"},
            files={"file": ("selected.md", b"# Choice\nOnly selected content.", "text/markdown")},
        )
        document_id = upload.json()["document_id"]
        await app.state.knowledge.knowledge.ingest(
            filename="unrelated.md", data=b"Do not include unrelated source."
        )
        response = await client.post(
            f"/v1/sessions/{session}/messages",
            json={"text": "解释这个附件", "media_ids": [document_id]},
        )
        assert response.status_code == 200
        answer = response.json()
        assert answer["intent"] == "knowledge_query"
        assert {item["source"] for item in answer["citations"]} == {"selected.md"}
        run = await client.get(f"/v1/runs/{answer['run_id']}")
        assert run.json() == answer
        verified = await client.get(f"/v1/traces/{answer['trace_id']}/verify")
        assert verified.json()["valid"] and verified.json()["events"] == 5
        trace_path = tmp_path / "traces" / f"{answer['trace_id']}.jsonl"
        events = [json.loads(line)["event"] for line in trace_path.read_text().splitlines()]
        assert events[1]["type"] == "retrieval.completed"
        assert events[1]["payload"]["final"][0]["chunk"]["document_id"] == document_id
        assert "Only selected content." in events[2]["payload"]["user"]
        assert "unrelated source" not in events[2]["payload"]["user"]
        restarted = KnowledgeRunStore(tmp_path / "knowledge.sqlite3", tmp_path / "traces")
        assert restarted.get(answer["run_id"]).model_dump(mode="json") == answer  # type: ignore[union-attr]
        trace_path.write_text(trace_path.read_text().replace("selected.md", "invented.md"))
        assert not verify_trace(trace_path).valid
    await app.state.knowledge.close()


async def test_unknown_unindexed_and_image_attachments(tmp_path: Path) -> None:
    application = build_knowledge_application(tmp_path)
    session = await application.sessions.create()
    with pytest.raises(ValueError, match="unknown attachment"):
        await application.conversations.send(
            session_id=session.id, text="show", media_ids=("0" * 64,)
        )
    document, _ = application.parser.parse(filename="pending.txt", data=b"pending")
    application.store.put(document, b"pending")
    with pytest.raises(ValueError, match="not been indexed"):
        await application.conversations.send(
            session_id=session.id, text="show", media_ids=(document.id,)
        )
    assert not await application.sessions.messages(session.id)
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buffer, format="PNG")
    image_document, _, _ = await application.knowledge.ingest(
        filename="screen.png", data=buffer.getvalue()
    )
    response = await application.conversations.send(
        session_id=session.id, text="解释截图", media_ids=(image_document.id,)
    )
    assert response.citations[0].source == "screen.png"
    history = await application.sessions.messages(session.id)
    assert history[0].parts[1].type == "image"
    assert history[0].parts[1].mime_type == "image/png"
    with pytest.raises(ValueError, match="limit"):
        SQLiteLexicalIndex(tmp_path / "knowledge-fts.sqlite3").document_chunks(document.id, limit=0)
    await application.close()


async def test_rag_failure_has_a_real_trace_and_no_success_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = build_knowledge_application(tmp_path)
    await application.knowledge.ingest(filename="guide.md", data=b"agent approval")

    async def unavailable(*, system: str, user: str) -> str:
        raise TimeoutError("model unavailable")

    monkeypatch.setattr(application.knowledge._chat, "complete", unavailable)
    with pytest.raises(TimeoutError):
        await application.knowledge.answer("agent approval")
    paths = list((tmp_path / "traces").glob("*.jsonl"))
    assert len(paths) == 1
    events = [json.loads(line)["event"] for line in paths[0].read_text().splitlines()]
    assert events[-1]["type"] == "knowledge.failed"
    assert events[-1]["payload"]["error_type"] == "TimeoutError"
    assert verify_trace(paths[0]).valid
    assert application.runs.get(paths[0].stem) is None
    await application.close()

"""Tests for intent dispatch and the public knowledge-agent API."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from forgeharness.api import create_app
from forgeharness.knowledge.application import build_knowledge_application
from forgeharness.knowledge.conversation import ModelIntentRouter, RuleIntentRouter
from forgeharness.knowledge.models import Chunk, Intent, JobStatus
from forgeharness.knowledge.testing import DeterministicChatModel
from forgeharness.models.omlx import OMLXProtocolError


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("请修复这个 bug", Intent.CODING_TASK),
        ("记住我使用 pytest", Intent.MEMORY_COMMAND),
        ("项目文档在哪里", Intent.KNOWLEDGE_QUERY),
        ("hello", Intent.GENERAL_CHAT),
        ("这是什么\N{FULLWIDTH QUESTION MARK}", Intent.KNOWLEDGE_QUERY),
    ],
)
async def test_rule_intent_router(text: str, expected: Intent) -> None:
    assert await RuleIntentRouter().classify(text) == expected


class _IntentModel:
    @property
    def model_name(self) -> str:
        return "intent"

    async def complete(self, *, system: str, user: str) -> str:
        del system
        if user == "valid":
            return 'prefix {"intent":"coding_task"} suffix'
        if user == "missing-key":
            return "{}"
        return "not json"

    async def describe_image(self, *, data: bytes, mime_type: str) -> str:
        del data, mime_type
        return "unused"


async def test_model_intent_router_uses_json_and_fallback() -> None:
    router = ModelIntentRouter(_IntentModel(), RuleIntentRouter())
    assert await router.classify("valid") == Intent.CODING_TASK
    assert await router.classify("missing-key") == Intent.GENERAL_CHAT
    assert await router.classify("项目文档在哪里") == Intent.KNOWLEDGE_QUERY


async def test_model_intent_router_recovers_from_network_errors() -> None:
    class UnavailableModel(_IntentModel):
        async def complete(self, *, system: str, user: str) -> str:
            raise OMLXProtocolError("model request timed out")

    router = ModelIntentRouter(UnavailableModel(), RuleIntentRouter())
    assert await router.classify("请修复 bug") == Intent.CODING_TASK


async def test_local_api_host_origin_and_browser_boundary(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001"
    ) as client:
        response = await client.get("/")
        script = response.text.split("<script>", 1)[1].split("</script>", 1)[0]
        expected_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{expected_hash}'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert 'type="file"' in response.text
        assert "media_ids: mediaIds" in script
        assert ".textContent" in script and ".innerHTML" not in script
        assert 'onclick="' not in response.text
        assert (
            await client.get("/health/live", headers={"host": "attacker.example"})
        ).status_code == 400
        assert (
            await client.post("/v1/sessions", headers={"origin": "https://attacker.example"})
        ).status_code == 403
        assert (
            await client.post("/v1/sessions", headers={"origin": "http://localhost:8001"})
        ).status_code == 201


async def test_conversation_routes_all_workflows(tmp_path: Path) -> None:
    application = build_knowledge_application(tmp_path)
    session = await application.sessions.create()
    await application.knowledge.ingest(
        filename="guide.md", data=b"# Agent\nagent tools use approval schema"
    )
    knowledge = await application.conversations.send(
        session_id=session.id, text="项目文档中的 agent tools 是什么?"
    )
    assert knowledge.intent == Intent.KNOWLEDGE_QUERY
    assert knowledge.citations

    coding = await application.conversations.send(session_id=session.id, text="请修复这个 bug")
    assert coding.intent == Intent.CODING_TASK
    assert "forge repair" in coding.answer

    memory = await application.conversations.send(session_id=session.id, text="记住我使用 pytest")
    assert memory.intent == Intent.MEMORY_COMMAND
    assert "候选记忆" in memory.answer

    chat = await application.conversations.send(session_id=session.id, text="你好")
    assert chat.intent == Intent.GENERAL_CHAT
    assert chat.model == "deterministic-chat"
    assert len(await application.sessions.messages(session.id)) == 8
    with pytest.raises(ValueError, match="unknown session"):
        await application.conversations.send(session_id="missing", text="hello")
    with pytest.raises(ValueError, match="must not be empty"):
        await application.conversations.send(session_id=session.id, text=" ")
    await application.close()


async def test_knowledge_api_upload_search_converse_review_and_metrics(tmp_path: Path) -> None:
    app = create_app(tmp_path / "data")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/health/live")).json()["status"] == "ok"
        ready = await client.get("/health/ready")
        assert ready.json()["status"] == "ready"
        assert ready.json()["mode"] == "keyless"

        upload = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "guide-v1"},
            files={"file": ("guide.md", b"# Agent\nagent tools validate schema", "text/markdown")},
        )
        assert upload.status_code == 202
        job_id = upload.json()["id"]
        job = await client.get(f"/v1/jobs/{job_id}")
        assert job.json()["status"] == JobStatus.SUCCEEDED

        duplicate = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "guide-v1"},
            files={"file": ("guide.md", b"# Agent\nagent tools validate schema")},
        )
        assert duplicate.json()["id"] == job_id
        same_content_new_key = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "guide-v1-second-key"},
            files={"file": ("guide.md", b"# Agent\nagent tools validate schema")},
        )
        assert same_content_new_key.json()["id"] == job_id
        conflict = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "guide-v1"},
            files={"file": ("other.md", b"different")},
        )
        assert conflict.status_code == 409

        search = await client.post("/v1/search", json={"query": "agent tools", "limit": 3})
        assert search.status_code == 200
        assert search.json()["final"][0]["chunk"]["source"] == "guide.md"

        created = await client.post("/v1/sessions")
        assert created.status_code == 201
        session_id = created.json()["id"]
        answer = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"text": "文档中的 agent tools 是什么?"},
        )
        assert answer.json()["intent"] == "knowledge_query"
        assert answer.json()["citations"]

        memory = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"text": "记住我使用 pytest"},
        )
        memory_id = re.search(r"[0-9a-f-]{36}", memory.json()["answer"])
        assert memory_id is not None
        reviewed = await client.post(
            f"/v1/memories/{memory_id.group(0)}/review", json={"approve": True}
        )
        assert reviewed.json()["status"] == "approved"
        remembered = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"text": "为什么要使用 pytest?"},
        )
        assert any(
            citation["source"].startswith("memory:") for citation in remembered.json()["citations"]
        )
        again = await client.post(
            f"/v1/memories/{memory_id.group(0)}/review", json={"approve": True}
        )
        assert again.status_code == 409

        revoked = await client.post(
            f"/v1/memories/{memory_id.group(0)}/review", json={"approve": False}
        )
        assert revoked.json()["status"] == "rejected"
        assert not (await app.state.knowledge.semantic_memories.search("pytest")).final
        deleted = await client.delete(f"/v1/memories/{memory_id.group(0)}")
        assert deleted.json()["status"] == "deleted"
        assert (await client.delete(f"/v1/memories/{memory_id.group(0)}")).status_code == 200
        assert (await client.delete("/v1/memories/missing")).status_code == 404

        loaded = await client.get(f"/v1/sessions/{session_id}")
        assert len(loaded.json()["messages"]) == 6
        metrics = await client.get("/metrics")
        assert "forgeharness_ingest_succeeded_total 1" in metrics.text


async def test_memory_revocation_suppresses_stale_vectors_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = build_knowledge_application(tmp_path)
    records = application.semantic_memories.records
    record = records.propose(task_id="test", content="use pytest", evidence_ref="test://memory")
    await application.semantic_memories.review(record.id, approve=True)
    assert (await application.semantic_memories.search("pytest")).final
    original_delete = application.memory_vector.delete

    async def unavailable(chunk_ids: Sequence[str]) -> None:
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(application.memory_vector, "delete", unavailable)
    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        await application.semantic_memories.revoke(record.id, deleted=True)
    assert not (await application.semantic_memories.search("pytest")).final
    assert not (await application.knowledge.search("pytest")).final
    answer = await application.knowledge.answer("pytest")
    assert not answer.citations
    assert answer.answer.startswith("不知道")
    monkeypatch.setattr(application.memory_vector, "delete", original_delete)
    await application.semantic_memories.revoke(record.id, deleted=True)
    await application.close()


async def test_knowledge_api_validation_and_missing_resources(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=create_app(tmp_path / "data"))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        no_key = await client.post("/v1/documents", files={"file": ("guide.md", b"text")})
        assert no_key.status_code == 422
        bad_file = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "bad"},
            files={"file": ("secret.exe", b"x")},
        )
        assert bad_file.status_code == 422
        assert (await client.get("/v1/jobs/missing")).status_code == 404
        assert (await client.get("/v1/sessions/missing")).status_code == 404
        assert (
            await client.post("/v1/sessions/missing/messages", json={"text": "hello"})
        ).status_code == 404
        assert (
            await client.post("/v1/memories/missing/review", json={"approve": False})
        ).status_code == 404

        demo = await client.post("/runs/keyless-demo", json={"text": "approval"})
        task_id = demo.json()["task_id"]
        approval = await client.post(
            f"/v1/runs/{task_id}/approve",
            json={"granted_by": "tester", "checkpoint_revision": 1},
        )
        assert approval.status_code == 409
        assert (
            await client.post(
                "/v1/runs/missing/approve",
                json={"granted_by": "x", "checkpoint_revision": 1},
            )
        ).status_code == 404
        assert (await client.get("/v1/traces/missing/verify")).status_code == 404


async def test_api_storage_failure_is_retryable_and_readiness_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path)
    application = app.state.knowledge
    original_put = application.store.put

    def failed_put(*args: object, **kwargs: object) -> bool:
        raise OSError("disk is temporarily unavailable")

    async def failed_ping() -> bool:
        return False

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        monkeypatch.setattr(application.store, "put", failed_put)
        first = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "disk-retry"},
            files={"file": ("guide.md", b"agent tools")},
        )
        assert first.status_code == 503
        monkeypatch.setattr(application.store, "put", original_put)
        retried = await client.post(
            "/v1/documents",
            headers={"Idempotency-Key": "disk-retry"},
            files={"file": ("guide.md", b"agent tools")},
        )
        assert retried.status_code == 202 and retried.json()["status"] == "succeeded"
        monkeypatch.setattr(application.queue, "ping", failed_ping)
        ready = await client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "not_ready"
    await application.close()


async def test_application_can_build_omlx_mode_without_contacting_server(tmp_path: Path) -> None:
    from forgeharness.knowledge.application import KnowledgeSettings

    application = build_knowledge_application(
        tmp_path,
        KnowledgeSettings(enable_omlx=True, omlx_base_url="http://127.0.0.1:1/v1"),
    )
    assert application.mode == "omlx"
    assert application.omlx is not None
    await application.close()


async def test_session_workspace_binding_is_scoped_to_configured_git_root(tmp_path: Path) -> None:
    from forgeharness.knowledge.application import KnowledgeSettings

    root = tmp_path / "workspaces"
    repository = root / "demo"
    (repository / ".git").mkdir(parents=True)
    settings = KnowledgeSettings(coding_workspace_root=root)
    app = create_app(tmp_path / "data", settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        created = await client.post("/v1/sessions", json={"workspace": "demo"})
        assert created.status_code == 201
        session_id = created.json()["id"]
        assert app.state.knowledge.store.get_workspace(session_id) == repository
        escaped = await client.post("/v1/sessions", json={"workspace": "../outside"})
        assert escaped.status_code == 422

    disabled = create_app(tmp_path / "disabled")
    disabled_transport = httpx.ASGITransport(app=disabled)
    async with httpx.AsyncClient(
        transport=disabled_transport, base_url="http://localhost"
    ) as client:
        assert (await client.post("/v1/sessions", json={"workspace": "demo"})).status_code == 403


async def test_deterministic_chat_image_description() -> None:
    model = DeterministicChatModel()
    description = await model.describe_image(data=b"abc", mime_type="image/png")
    assert "bytes=3" in description
    assert await model.complete(system="s", user="") == "不知道: 没有检索到足够证据。"


async def test_semantic_memory_approval_can_retry_failed_vector_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = build_knowledge_application(tmp_path)
    record = application.memories.propose(
        task_id="task-1",
        content="Use pytest for regression tests",
        evidence_ref="trace:task-1",
    )
    vector = application.semantic_memories._vector
    original_upsert = vector.upsert
    calls = 0

    async def fail_once(chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("vector unavailable")
        await original_upsert(chunks, vectors)

    monkeypatch.setattr(vector, "upsert", fail_once)
    with pytest.raises(RuntimeError, match="vector unavailable"):
        await application.semantic_memories.review(record.id, approve=True)
    assert application.memories.get(record.id).status.value == "approved"  # type: ignore[union-attr]
    assert application.memories.is_indexed(record.id) is False

    retried = await application.semantic_memories.review(record.id, approve=True)
    assert retried.status.value == "approved"
    assert application.memories.is_indexed(record.id) is True
    with pytest.raises(RuntimeError, match="already approved"):
        await application.semantic_memories.review(record.id, approve=True)
    await application.close()

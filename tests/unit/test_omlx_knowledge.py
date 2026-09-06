"""Protocol tests for the local OMLX model adapters."""

from __future__ import annotations

import httpx
import pytest

from forgeharness.models.omlx import (
    OMLXChatModel,
    OMLXClient,
    OMLXConfig,
    OMLXEmbeddingModel,
    OMLXProtocolError,
    OMLXReranker,
)


async def test_omlx_inventory_chat_embedding_rerank_and_vision() -> None:
    seen_authorization: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "chat"},
                        {"id": "embed"},
                        {"id": "rerank"},
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/chat/completions":
            body = request.content.decode()
            content = "image evidence" if "image_url" in body else "grounded answer"
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}]},
                request=request,
            )
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/rerank":
            return httpx.Response(
                200,
                json={"results": [{"index": 1, "relevance_score": 0.9}]},
                request=request,
            )
        raise AssertionError(request.url.path)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OMLXClient(
        OMLXConfig(
            base_url="http://omlx/v1",
            api_key="secret",
            chat_model="chat",
            embedding_model="embed",
            reranker_model="rerank",
        ),
        client=http,
    )
    capabilities = await client.capabilities()
    assert all(item.available for item in capabilities)
    chat = OMLXChatModel(client)
    assert chat.model_name == "chat"
    assert await chat.complete(system="ground", user="question") == "grounded answer"
    assert await chat.describe_image(data=b"png", mime_type="image/png") == "image evidence"
    embedding = OMLXEmbeddingModel(client)
    assert embedding.model_name == "embed"
    assert await embedding.embed(["first", "second"]) == ((1.0, 0.0), (0.0, 1.0))
    reranker = OMLXReranker(client)
    assert reranker.model_name == "rerank"
    assert await reranker.rerank("q", ["a", "b"], limit=1) == ((1, 0.9),)
    assert set(seen_authorization) == {"Bearer secret"}
    await client.close()
    await http.aclose()


async def test_omlx_marks_missing_models_and_validates_inputs() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("models"):
            return httpx.Response(200, json={"data": [{"id": "chat"}]}, request=request)
        raise AssertionError(request.url.path)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OMLXClient(
        OMLXConfig(chat_model="chat", embedding_model="missing-e", reranker_model="missing-r"),
        client=http,
    )
    capabilities = await client.capabilities()
    assert [item.available for item in capabilities] == [True, False, False]
    with pytest.raises(ValueError, match="non-empty text"):
        await OMLXEmbeddingModel(client).embed([])
    with pytest.raises(ValueError, match="query and documents"):
        await OMLXReranker(client).rerank("", ["a"], limit=1)
    with pytest.raises(ValueError, match="fit the document count"):
        await OMLXReranker(client).rerank("q", ["a"], limit=2)
    await http.aclose()


@pytest.mark.parametrize(
    ("path", "payload", "operation", "message"),
    [
        ("/models", {"bad": []}, "capabilities", "inventory"),
        (
            "/chat/completions",
            {"choices": [{"message": {"content": ""}}]},
            "chat",
            "non-empty",
        ),
        (
            "/embeddings",
            {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": []}]},
            "embedding",
            "dimension",
        ),
        (
            "/rerank",
            {"results": [{"index": 4, "relevance_score": 1.0}]},
            "rerank",
            "out-of-range",
        ),
    ],
)
async def test_omlx_rejects_malformed_responses(
    path: str, payload: dict[str, object], operation: str, message: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(path)
        return httpx.Response(200, json=payload, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OMLXClient(OMLXConfig(), client=http)
    with pytest.raises(OMLXProtocolError, match=message):
        if operation == "capabilities":
            await client.capabilities()
        elif operation == "chat":
            await OMLXChatModel(client).complete(system="s", user="u")
        elif operation == "embedding":
            await OMLXEmbeddingModel(client).embed(["a", "b"])
        else:
            await OMLXReranker(client).rerank("q", ["a"], limit=1)
    await http.aclose()


async def test_omlx_wraps_http_errors_and_embedding_count_mismatch() -> None:
    async def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    client = OMLXClient(OMLXConfig(), client=http)
    with pytest.raises(OMLXProtocolError, match="request failed"):
        await client.capabilities()
    await http.aclose()

    async def short(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0]}]},
            request=request,
        )

    short_http = httpx.AsyncClient(transport=httpx.MockTransport(short))
    short_client = OMLXClient(OMLXConfig(), client=short_http)
    with pytest.raises(OMLXProtocolError, match="count differs"):
        await OMLXEmbeddingModel(short_client).embed(["a", "b"])
    await short_http.aclose()

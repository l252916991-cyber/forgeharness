"""Tests for reproducible OMLX qualification reporting."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from forgeharness.evaluation.omlx import _image, run_omlx_qualification
from forgeharness.models.omlx import OMLXConfig


async def test_quick_omlx_qualification_writes_complete_report(tmp_path: Path) -> None:
    vision_labels = iter(("ERROR 401", "ERROR 403"))

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "chat"}, {"id": "embed"}, {"id": "rerank"}]},
                request=request,
            )
        body = json.loads(request.content)
        if path == "/v1/chat/completions" and "tools" in body:
            token = body["messages"][-1]["content"].split('"')[1]
            return httpx.Response(
                200,
                json={
                    "model": "chat",
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "echo",
                                            "arguments": json.dumps({"text": token}),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
                request=request,
            )
        if path == "/v1/chat/completions":
            content = body["messages"][-1]["content"]
            if isinstance(content, list):
                answer = next(vision_labels)
            else:
                answer = '{"intent":"knowledge_query"}'
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": answer}}]},
                request=request,
            )
        if path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0, 0.0]},
                        {"index": 1, "embedding": [0.9, 0.1]},
                        {"index": 2, "embedding": [0.0, 1.0]},
                    ]
                },
                request=request,
            )
        if path == "/v1/rerank":
            return httpx.Response(
                200,
                json={"results": [{"index": 1, "relevance_score": 0.9}]},
                request=request,
            )
        raise AssertionError(path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    output = tmp_path / "qualification.json"
    report = await run_omlx_qualification(
        config=OMLXConfig(
            base_url="http://omlx/v1",
            chat_model="chat",
            embedding_model="embed",
            reranker_model="rerank",
        ),
        output_path=output,
        project_root=Path.cwd(),
        quick=True,
        client=client,
    )
    assert report.qualified is True
    assert all(section.total == 2 for section in report.sections.values())
    assert json.loads(output.read_text())["qualified"] is True
    await client.aclose()


async def test_omlx_qualification_records_inventory_failure(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="model server unavailable", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    output = tmp_path / "qualification-failed.json"
    report = await run_omlx_qualification(
        config=OMLXConfig(base_url="http://omlx/v1"),
        output_path=output,
        project_root=Path.cwd(),
        quick=True,
        client=client,
    )
    assert report.qualified is False
    assert report.sections == {}
    assert report.fatal_error is not None
    assert "502" in report.fatal_error
    assert json.loads(output.read_text())["qualified"] is False
    await client.aclose()


def test_qualification_image_uses_readable_text() -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(_image("ERROR 409")))
    assert image.size == (720, 220)
    assert sum(image.convert("L").histogram()[:128]) > 1_000

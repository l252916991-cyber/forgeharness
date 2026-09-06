"""Typed OMLX adapters for chat, embeddings, reranking, and readiness."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forgeharness.knowledge.models import ModelCapability


class OMLXProtocolError(RuntimeError):
    """OMLX returned a response that cannot safely enter application state."""


class OMLXConfig(BaseModel):
    """Validated configuration for one local multi-model OMLX server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str | None = None
    chat_model: str = "Qwen3.5-9B-4bit"
    embedding_model: str = "Qwen3-Embedding-4B-4bit-DWQ"
    reranker_model: str = "bge-reranker-v2-m3-mlx"
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)


class _ModelItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str


class _ModelList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: list[_ModelItem]


class _EmbeddingItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    embedding: list[float]


class _EmbeddingResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: list[_EmbeddingItem]


class _RerankItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    relevance_score: float


class _RerankResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    results: list[_RerankItem]


class OMLXClient:
    """Share connection policy while exposing narrow application adapters."""

    def __init__(self, config: OMLXConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        # OMLX is a loopback-only service. Ignoring system proxy settings prevents
        # localhost requests from being sent through a desktop HTTP proxy.
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds, trust_env=False)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def capabilities(self) -> tuple[ModelCapability, ...]:
        response = await self._request("GET", "/models")
        try:
            model_list = _ModelList.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise OMLXProtocolError(f"invalid model inventory: {exc}") from exc
        available = {item.id for item in model_list.data}
        configured: tuple[tuple[str, Literal["chat", "embedding", "reranker"]], ...] = (
            (self.config.chat_model, "chat"),
            (self.config.embedding_model, "embedding"),
            (self.config.reranker_model, "reranker"),
        )
        return tuple(
            ModelCapability(model_id=model, role=role, available=model in available)
            for model, role in configured
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            response = await self._client.request(
                method,
                f"{self.config.base_url.rstrip('/')}{path}",
                headers=headers,
                timeout=self.config.timeout_seconds,
                **kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OMLXProtocolError(f"OMLX request failed for {path}: {exc}") from exc
        return response


class OMLXChatModel:
    """Text and image generation through the OMLX chat-completions endpoint."""

    def __init__(self, client: OMLXClient) -> None:
        self._omlx = client

    @property
    def model_name(self) -> str:
        return self._omlx.config.chat_model

    async def complete(self, *, system: str, user: str) -> str:
        return await self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=1_200,
        )

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> str:
        """Generate short JSON with OMLX grammar enforcement and thinking disabled."""
        return await self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=96,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "forgeharness_response",
                    "strict": True,
                    "schema": schema,
                },
            },
        )

    async def describe_image(self, *, data: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        return await self._chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract visible text, code, errors, UI elements, and relationships. "
                        "Describe only observable evidence; never invent hidden content."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Create a searchable description."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            max_tokens=512,
        )

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format is not None:
            payload["response_format"] = response_format
        response = await self._omlx._request(
            "POST",
            "/chat/completions",
            json=payload,
        )
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OMLXProtocolError("chat response contained no validated text") from exc
        if not isinstance(content, str) or not content.strip():
            raise OMLXProtocolError("chat response content must be non-empty text")
        return content.strip()


class OMLXEmbeddingModel:
    """Batch text embeddings with stable ordering and dimension validation."""

    def __init__(self, client: OMLXClient) -> None:
        self._omlx = client

    @property
    def model_name(self) -> str:
        return self._omlx.config.embedding_model

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("embedding input must contain non-empty text")
        response = await self._omlx._request(
            "POST", "/embeddings", json={"model": self.model_name, "input": list(texts)}
        )
        try:
            parsed = _EmbeddingResponse.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise OMLXProtocolError(f"invalid embedding response: {exc}") from exc
        ordered = sorted(parsed.data, key=lambda item: item.index)
        if len(ordered) != len(texts):
            raise OMLXProtocolError("embedding response count differs from input count")
        dimensions = {len(item.embedding) for item in ordered}
        if len(dimensions) != 1 or not dimensions or next(iter(dimensions)) == 0:
            raise OMLXProtocolError("embedding vectors must share one non-zero dimension")
        return tuple(tuple(item.embedding) for item in ordered)


class OMLXReranker:
    """Cross-encoder reranking through OMLX's dedicated endpoint."""

    def __init__(self, client: OMLXClient) -> None:
        self._omlx = client

    @property
    def model_name(self) -> str:
        return self._omlx.config.reranker_model

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> tuple[tuple[int, float], ...]:
        if not query.strip() or not documents:
            raise ValueError("reranking requires a query and documents")
        if not 1 <= limit <= len(documents):
            raise ValueError("rerank limit must fit the document count")
        response = await self._omlx._request(
            "POST",
            "/rerank",
            json={
                "model": self.model_name,
                "query": query,
                "documents": list(documents),
                "top_n": limit,
            },
        )
        try:
            parsed = _RerankResponse.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise OMLXProtocolError(f"invalid rerank response: {exc}") from exc
        result = tuple((item.index, item.relevance_score) for item in parsed.results)
        if any(index < 0 or index >= len(documents) for index, _ in result):
            raise OMLXProtocolError("reranker returned an out-of-range document index")
        return result[:limit]

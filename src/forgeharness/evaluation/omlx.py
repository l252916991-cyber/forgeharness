"""Reproducible local-model qualification for the configured OMLX server."""

from __future__ import annotations

import asyncio
import io
import json
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont
from pydantic import Field

from forgeharness.domain.models import FrozenModel, Message, MessageRole, ToolAction
from forgeharness.models.base import ModelRequest
from forgeharness.models.omlx import (
    OMLXChatModel,
    OMLXClient,
    OMLXConfig,
    OMLXEmbeddingModel,
    OMLXReranker,
)
from forgeharness.models.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleModel,
)
from forgeharness.tools.builtin import EchoTool


class QualificationSection(FrozenModel):
    """Per-capability outcomes with every attempted case retained."""

    passed: int = Field(ge=0)
    total: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    median_latency_ms: float = Field(ge=0)
    cases: tuple[dict[str, Any], ...]


class OMLXQualificationReport(FrozenModel):
    """Version- and configuration-linked model qualification evidence."""

    created_at: datetime
    revision: str
    quick: bool
    models: dict[str, str]
    inventory: dict[str, bool]
    sections: dict[str, QualificationSection]
    thresholds: dict[str, float]
    qualified: bool
    fatal_error: str | None = None


async def run_omlx_qualification(
    *,
    config: OMLXConfig,
    output_path: Path,
    project_root: Path,
    quick: bool = False,
    client: httpx.AsyncClient | None = None,
) -> OMLXQualificationReport:
    """Run bounded tool, intent, vision, embedding, and reranking checks."""
    http = client or httpx.AsyncClient(
        timeout=config.timeout_seconds,
        trust_env=False,
    )
    owns_client = client is None
    omlx = OMLXClient(config, client=http)
    thresholds = {
        "tool_calling": 0.90,
        "intent": 0.90,
        "vision": 0.85,
        "embedding": 0.90,
        "reranker": 0.80,
    }
    try:
        try:
            capabilities = await omlx.capabilities()
        except Exception as exc:
            report = OMLXQualificationReport(
                created_at=datetime.now(UTC),
                revision=_revision(project_root),
                quick=quick,
                models={
                    "chat": config.chat_model,
                    "embedding": config.embedding_model,
                    "reranker": config.reranker_model,
                },
                inventory={
                    config.chat_model: False,
                    config.embedding_model: False,
                    config.reranker_model: False,
                },
                sections={},
                thresholds=thresholds,
                qualified=False,
                fatal_error=f"{type(exc).__name__}: {exc}",
            )
            await asyncio.to_thread(_write_report, output_path, report)
            return report
        inventory = {item.model_id: item.available for item in capabilities}
        limit = 2 if quick else None
        sections = {
            "tool_calling": await _qualify_tools(config, http, limit=limit),
            "intent": await _qualify_intent(OMLXChatModel(omlx), limit=limit),
            "vision": await _qualify_vision(OMLXChatModel(omlx), limit=limit),
            "embedding": await _qualify_embedding(OMLXEmbeddingModel(omlx), limit=limit),
            "reranker": await _qualify_reranker(OMLXReranker(omlx), limit=limit),
        }
        qualified = all(inventory.values()) and all(
            sections[name].pass_rate >= threshold for name, threshold in thresholds.items()
        )
        report = OMLXQualificationReport(
            created_at=datetime.now(UTC),
            revision=_revision(project_root),
            quick=quick,
            models={
                "chat": config.chat_model,
                "embedding": config.embedding_model,
                "reranker": config.reranker_model,
            },
            inventory=inventory,
            sections=sections,
            thresholds=thresholds,
            qualified=qualified,
        )
        await asyncio.to_thread(_write_report, output_path, report)
        return report
    finally:
        if owns_client:
            await http.aclose()


async def _qualify_tools(
    config: OMLXConfig, http: httpx.AsyncClient, *, limit: int | None
) -> QualificationSection:
    model = OpenAICompatibleModel(
        OpenAICompatibleConfig(
            base_url=config.base_url,
            api_key=config.api_key or "omlx-local",
            model=config.chat_model,
            timeout_seconds=config.timeout_seconds,
            temperature=0,
            max_tokens=128,
            enable_thinking=False,
        ),
        client=http,
    )
    cases = [f"qualification-{index:02d}" for index in range(1, 21)][:limit]
    outcomes = []
    for token in cases:
        started = time.perf_counter()
        passed = False
        error = None
        actual: Any = None
        try:
            result = await model.decide(
                ModelRequest(
                    task_id=token,
                    messages=(
                        Message(
                            role=MessageRole.SYSTEM,
                            content=(
                                "Call the echo tool exactly once. Copy the exact text argument "
                                "from the user; do not add, remove, or translate characters."
                            ),
                        ),
                        Message(
                            role=MessageRole.USER,
                            content=f'The exact text argument is "{token}".',
                        ),
                    ),
                    tools=(EchoTool().spec,),
                )
            )
            action = result.action
            actual = (
                {"tool": action.call.name, "arguments": action.call.arguments}
                if isinstance(action, ToolAction)
                else {"kind": action.kind, "content": action.content}
            )
            passed = (
                isinstance(action, ToolAction)
                and action.call.name == "echo"
                and action.call.arguments.get("text") == token
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        outcomes.append(
            _outcome(
                token,
                passed,
                started,
                error,
                {"tool": "echo", "arguments": {"text": token}},
                actual,
            )
        )
    return _section(outcomes)


async def _qualify_intent(model: OMLXChatModel, *, limit: int | None) -> QualificationSection:
    cases = [
        ("Where is the checkpoint implementation?", "knowledge_query"),
        ("Explain the approval policy from the docs", "knowledge_query"),
        ("Find the source of the tool registry", "knowledge_query"),
        ("What does the architecture document say?", "knowledge_query"),
        ("How is context compressed?", "knowledge_query"),
        ("Fix the failing unit test", "coding_task"),
        ("Implement a new parser", "coding_task"),
        ("Refactor the dispatcher", "coding_task"),
        ("Run tests and repair the bug", "coding_task"),
        ("Change this function safely", "coding_task"),
        ("Remember that this repo uses uv", "memory_command"),
        ("Store this as long-term memory", "memory_command"),
        ("Forget my previous preference", "memory_command"),
        ("记住我使用pytest", "memory_command"),
        ("Save this lesson for later", "memory_command"),
        ("Hello", "general_chat"),
        ("Tell me a short greeting", "general_chat"),
        ("Who are you?", "general_chat"),
        ("Thanks", "general_chat"),
        ("Good morning", "general_chat"),
    ][:limit]
    outcomes = []
    for index, (text, expected) in enumerate(cases, start=1):
        started = time.perf_counter()
        error = None
        actual = None
        try:
            response = await model.complete_json(
                system=(
                    "Return JSON only with one intent: knowledge_query, coding_task, "
                    'memory_command, or general_chat. Example: {"intent":"general_chat"}.'
                ),
                user=text,
                schema={
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "enum": [
                                "knowledge_query",
                                "coding_task",
                                "memory_command",
                                "general_chat",
                            ],
                        }
                    },
                    "required": ["intent"],
                    "additionalProperties": False,
                },
            )
            actual = json.loads(_json_object(response)).get("intent")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        outcomes.append(
            _outcome(f"intent-{index:02d}", actual == expected, started, error, expected, actual)
        )
    return _section(outcomes)


async def _qualify_vision(model: OMLXChatModel, *, limit: int | None) -> QualificationSection:
    labels = [
        "ERROR 401",
        "ERROR 403",
        "ERROR 404",
        "ERROR 409",
        "ERROR 422",
        "ERROR 429",
        "ERROR 500",
        "TIMEOUT 30S",
        "TRACE INVALID",
        "TEST FAILED",
        "BUILD PASSED",
        "AGENT LOOP",
        "TOOL POLICY",
        "VECTOR SEARCH",
        "MEMORY REVIEW",
        "REDIS QUEUE",
        "POSTGRES READY",
        "QDRANT READY",
        "APPROVAL NEEDED",
        "CHECKPOINT SAVED",
    ][:limit]
    outcomes = []
    for index, label in enumerate(labels, start=1):
        started = time.perf_counter()
        error = None
        actual = None
        try:
            actual = await model.describe_image(data=_image(label), mime_type="image/png")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        passed = actual is not None and all(token in actual.upper() for token in label.split())
        outcomes.append(_outcome(f"vision-{index:02d}", passed, started, error, label, actual))
    return _section(outcomes)


async def _qualify_embedding(
    model: OMLXEmbeddingModel, *, limit: int | None
) -> QualificationSection:
    cases = [
        ("agent tool schema", "tool calling schema", "banana recipe"),
        ("vector retrieval", "semantic vector search", "HTTP status code"),
        ("approval policy", "permission approval", "image pixels"),
        ("checkpoint recovery", "resume saved state", "music player"),
        ("long term memory", "remember approved lesson", "database port"),
        ("context compression", "shorten conversation history", "CSS color"),
        ("document chunk", "split source document", "weather forecast"),
        ("citation evidence", "grounded source reference", "shell command"),
        ("multi agent reviewer", "agent review collaboration", "PDF page size"),
        ("intent routing", "classify workflow intent", "coffee beans"),
    ][:limit]
    outcomes = []
    for index, (anchor, positive, negative) in enumerate(cases, start=1):
        started = time.perf_counter()
        error = None
        details = None
        passed = False
        try:
            vectors = await model.embed([anchor, positive, negative])
            related = _cosine(vectors[0], vectors[1])
            unrelated = _cosine(vectors[0], vectors[2])
            passed = related > unrelated
            details = {"related": related, "unrelated": unrelated, "dimension": len(vectors[0])}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        outcomes.append(_outcome(f"embedding-{index:02d}", passed, started, error, actual=details))
    return _section(outcomes)


async def _qualify_reranker(model: OMLXReranker, *, limit: int | None) -> QualificationSection:
    topics = [
        "agent loop",
        "tool schema",
        "approval policy",
        "context budget",
        "vector retrieval",
        "document chunking",
        "citation validation",
        "memory review",
        "checkpoint recovery",
        "trace verification",
        "intent routing",
        "session storage",
        "redis queue",
        "postgres transaction",
        "qdrant collection",
        "fastapi readiness",
        "model timeout",
        "multi agent reviewer",
        "MCP tools",
        "skills loader",
        "path security",
        "subprocess timeout",
        "hash chain",
        "reranker model",
        "embedding dimension",
        "hybrid search",
        "reciprocal rank fusion",
        "idempotency key",
        "background worker",
        "structured output",
    ][:limit]
    outcomes = []
    for index, topic in enumerate(topics, start=1):
        started = time.perf_counter()
        error = None
        actual = None
        documents = ["unrelated cooking notes", f"technical documentation about {topic}"]
        try:
            ranking = await model.rerank(topic, documents, limit=1)
            actual = ranking[0][0] if ranking else None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        outcomes.append(_outcome(f"rerank-{index:02d}", actual == 1, started, error, 1, actual))
    return _section(outcomes)


def _outcome(
    case_id: str,
    passed: bool,
    started: float,
    error: str | None = None,
    expected: Any = None,
    actual: Any = None,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "passed": passed,
        "latency_ms": round((time.perf_counter() - started) * 1_000, 3),
        "error": error,
        "expected": expected,
        "actual": actual,
    }


def _section(outcomes: list[dict[str, Any]]) -> QualificationSection:
    passed = sum(bool(item["passed"]) for item in outcomes)
    latencies = [float(item["latency_ms"]) for item in outcomes]
    return QualificationSection(
        passed=passed,
        total=len(outcomes),
        pass_rate=passed / len(outcomes) if outcomes else 0,
        median_latency_ms=statistics.median(latencies) if latencies else 0,
        cases=tuple(outcomes),
    )


def _image(label: str) -> bytes:
    image = Image.new("RGB", (720, 220), "white")
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    ImageDraw.Draw(image).text((40, 75), label, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _json_object(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response has no JSON object")
    return text[start : end + 1]


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0


def _revision(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _write_report(path: Path, report: OMLXQualificationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

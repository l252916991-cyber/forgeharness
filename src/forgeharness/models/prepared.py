"""Inspectable model-call preparation without transport credentials or network I/O."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, model_validator

from forgeharness.domain.models import FrozenModel, Message, MessageRole, ModelResult
from forgeharness.models.base import Model, ModelRequest


def canonical_json(value: Any) -> str:
    """One deterministic JSON representation for sizing, trace and dispatch."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def chat_message(message: Message) -> dict[str, Any]:
    """Serialize the supported chat-completions message contract in one place."""
    result: dict[str, Any] = {"role": message.role.value}
    if message.content is not None or message.tool_calls:
        result["content"] = message.content
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": canonical_json(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.role == MessageRole.TOOL:
        result["tool_call_id"] = message.tool_call_id
    return result


def chat_inputs(request: ModelRequest) -> dict[str, Any]:
    """Canonical message/tool fields shared by adapter serialization and validation."""
    body: dict[str, Any] = {"messages": [chat_message(message) for message in request.messages]}
    if request.tools:
        body.update(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ],
            tool_choice="auto",
            parallel_tool_calls=False,
        )
    return body


class PreparedModelCall(FrozenModel):
    """Detached immutable JSON snapshots; headers and credentials never belong here."""

    request_json: str
    payload_json: str
    format: str = Field(default="model-request-v1", min_length=1, max_length=100)
    output_limit: int | None = Field(default=None, ge=1)
    context_window: int | None = Field(default=None, ge=1)
    provider_overhead_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_json(self) -> PreparedModelCall:
        """Untrusted adapter declarations must decode to validated JSON objects."""
        request = ModelRequest.model_validate_json(self.request_json)
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise ValueError("prepared model payload must be a JSON object")
        if canonical_json(value) != self.payload_json:
            raise ValueError("prepared model payload must use canonical JSON")
        if self.format == "model-request-v1":
            if value != request.model_dump(mode="json"):
                raise ValueError("prepared payload does not match its runtime request")
        elif self.format in {"chat-completions-v1", "chat-completions-cache-explicit-v1"}:
            expected = chat_inputs(request)
            if self.format == "chat-completions-cache-explicit-v1":
                # Local import keeps the cache planner's public prepared-call
                # utility free of a module import cycle.
                from forgeharness.models.caching import CacheMode, apply_cache_plan, plan_cache

                expected = apply_cache_plan(expected, plan_cache(request, mode=CacheMode.EXPLICIT))
            if any(value.get(key) != item for key, item in expected.items()):
                raise ValueError("prepared chat inputs do not match the runtime request")
            if not request.tools and value.get("tools"):
                raise ValueError("prepared chat payload adds undeclared tools")
            if self.output_limit != value.get("max_tokens"):
                raise ValueError("prepared output limit does not match its payload")
        return self

    @property
    def request(self) -> ModelRequest:
        """Return a fresh detached request, never a mutable schema retained by the caller."""
        return ModelRequest.model_validate_json(self.request_json)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)  # type: ignore[no-any-return]

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload_json.encode()).hexdigest()


@runtime_checkable
class PreparingModel(Protocol):
    """Optional adapter path: execute the exact body that passed the preflight."""

    def prepare_request(self, request: ModelRequest) -> PreparedModelCall: ...

    async def decide_prepared(self, prepared: PreparedModelCall) -> ModelResult: ...


def prepare_model_call(model: Model, request: ModelRequest) -> PreparedModelCall:
    """Prepare with an opted-in adapter, otherwise snapshot the public Model boundary."""
    request_json = canonical_json(request.model_dump(mode="json"))
    detached = ModelRequest.model_validate_json(request_json)
    if isinstance(model, PreparingModel):
        prepared = PreparedModelCall.model_validate(model.prepare_request(detached).model_dump())
        if canonical_json(prepared.request.model_dump(mode="json")) != request_json:
            raise ValueError("model preparation changed the runtime request")
        return prepared
    return PreparedModelCall(request_json=request_json, payload_json=request_json)


async def decide_prepared_model(model: Model, prepared: PreparedModelCall) -> ModelResult:
    """Never rebuild an opted-in adapter payload after budget admission."""
    if isinstance(model, PreparingModel):
        return await model.decide_prepared(prepared)
    return await model.decide(prepared.request)

"""Adapter for OpenAI-compatible chat-completions endpoints."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forgeharness.domain.models import (
    AgentAction,
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    ToolAction,
    ToolCall,
)
from forgeharness.models.base import ModelRequest


class ModelProtocolError(RuntimeError):
    """The remote endpoint returned a response the Harness cannot execute safely."""


class OpenAICompatibleConfig(BaseModel):
    """Validated configuration for an OpenAI-compatible endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    api_key: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32_768)
    enable_thinking: bool | None = None


class _FunctionCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: str


class _RemoteToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: str = "function"
    function: _FunctionCall


class _AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str | None = None
    tool_calls: list[_RemoteToolCall] = Field(default_factory=list)


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _AssistantMessage


class _RemoteUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0


class _ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "unknown"
    choices: list[_Choice]
    usage: _RemoteUsage = _RemoteUsage()


class OpenAICompatibleModel:
    """Translate Harness messages and tools to chat-completions requests."""

    def __init__(
        self, config: OpenAICompatibleConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    async def decide(self, request: ModelRequest) -> ModelResult:
        """Request and validate exactly one final answer or tool call."""
        response = await self._client.post(
            f"{self._config.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self._config.api_key}"},
            json=self._request_body(request),
            timeout=self._config.timeout_seconds,
        )
        response.raise_for_status()
        try:
            completion = _ChatCompletion.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise ModelProtocolError(f"invalid chat-completions response: {exc}") from exc
        if len(completion.choices) != 1:
            raise ModelProtocolError(
                f"expected exactly one completion choice, got {len(completion.choices)}"
            )
        remote_message = completion.choices[0].message
        if len(remote_message.tool_calls) > 1:
            raise ModelProtocolError("parallel tool calls are not enabled for this runtime")
        action: AgentAction
        if remote_message.tool_calls:
            action = ToolAction(call=self._parse_tool_call(remote_message.tool_calls[0]))
        elif remote_message.content is not None:
            action = FinalAction(content=remote_message.content)
        else:
            raise ModelProtocolError("completion contained neither content nor a tool call")
        return ModelResult(
            action=action,
            model_name=completion.model,
            usage=ModelUsage(
                input_tokens=completion.usage.prompt_tokens,
                output_tokens=completion.usage.completion_tokens,
            ),
        )

    def _request_body(self, request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._config.model,
            "temperature": self._config.temperature,
            "messages": [self._serialize_message(message) for message in request.messages],
        }
        if self._config.max_tokens is not None:
            body["max_tokens"] = self._config.max_tokens
        if self._config.enable_thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": self._config.enable_thinking}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
                for spec in request.tools
            ]
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False
        return body

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        serialized: dict[str, Any] = {"role": message.role.value}
        if message.content is not None:
            serialized["content"] = message.content
        if message.tool_calls:
            serialized["content"] = message.content
            serialized["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == MessageRole.TOOL:
            serialized["tool_call_id"] = message.tool_call_id
        return serialized

    @staticmethod
    def _parse_tool_call(remote: _RemoteToolCall) -> ToolCall:
        if remote.type != "function":
            raise ModelProtocolError(f"unsupported tool-call type: {remote.type}")
        try:
            arguments = json.loads(remote.function.arguments)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError(f"tool arguments are not valid JSON: {exc.msg}") from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError("tool arguments must decode to an object")
        try:
            return ToolCall(id=remote.id, name=remote.function.name, arguments=arguments)
        except ValidationError as exc:
            raise ModelProtocolError(f"invalid tool call: {exc}") from exc

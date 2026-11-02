"""Adapter for OpenAI-compatible chat-completions endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forgeharness.domain.models import (
    AgentAction,
    FinalAction,
    Message,
    ModelResult,
    ModelUsage,
    ToolAction,
    ToolCall,
)
from forgeharness.models.base import ModelProtocolError as SharedModelProtocolError
from forgeharness.models.base import ModelRequest
from forgeharness.models.caching import CacheMode, CachePlan, apply_cache_plan, plan_cache
from forgeharness.models.prepared import (
    PreparedModelCall,
    canonical_json,
    chat_inputs,
    chat_message,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(base_url: str) -> bool:
    """True when the endpoint is a loopback host, where a proxy is never correct."""
    host = urlparse(base_url).hostname
    return host in _LOOPBACK_HOSTS


class ModelProtocolError(SharedModelProtocolError):
    """Adapter-local name for the shared protocol violation contract.

    Kept as a subclass so existing imports keep working, while the runtime can
    still catch the provider-neutral base class without importing this module.
    """


class OpenAICompatibleConfig(BaseModel):
    """Validated configuration for an OpenAI-compatible endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    api_key: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    require_usage: bool = Field(default=False, strict=True)
    #: Bound untrusted HTTP bytes before JSON parsing or evidence staging.
    max_response_bytes: int = Field(
        default=8 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024, strict=True
    )
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32_768)
    enable_thinking: bool | None = None
    #: How ``enable_thinking`` is expressed on the wire. Local Qwen-style servers read
    #: ``chat_template_kwargs``; DeepSeek's hosted API reads a top-level ``thinking``
    #: object and, with thinking on, requires reasoning to be echoed back on every tool
    #: turn, which this adapter does not do.
    thinking_control: Literal["chat_template_kwargs", "thinking_object"] = "chat_template_kwargs"
    context_window_tokens: int | None = Field(default=None, ge=1)
    provider_overhead_tokens: int | None = Field(default=None, ge=0)
    #: How this endpoint wants a cacheable prefix expressed. Defaults to OFF because
    #: an unknown server may reject ``cache_control``; a gateway that caches prefixes
    #: automatically loses nothing by staying OFF, it only loses the report.
    cache_mode: CacheMode = CacheMode.OFF


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


class _PromptDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cached_tokens: int = Field(default=0, ge=0, strict=True)


class _RemoteUsage(BaseModel):
    """Provider usage, including whatever form of cache accounting it reports.

    Two spellings are in the wild for the same number: OpenAI-compatible gateways put
    it under ``prompt_tokens_details.cached_tokens``, Anthropic-flavoured ones report
    ``cache_read_input_tokens``. Both are optional and default to zero.
    """

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = Field(default=0, ge=0, strict=True)
    completion_tokens: int = Field(default=0, ge=0, strict=True)
    prompt_tokens_details: _PromptDetails | None = None
    cache_read_input_tokens: int = Field(default=0, ge=0, strict=True)

    @property
    def cached_tokens(self) -> int:
        """Prompt tokens the provider served from its cache, in either spelling."""
        nested = self.prompt_tokens_details.cached_tokens if self.prompt_tokens_details else 0
        return max(nested, self.cache_read_input_tokens)


class _ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "unknown"
    choices: list[_Choice]
    usage: _RemoteUsage | None = None


class OpenAICompatibleModel:
    """Translate Harness messages and tools to chat-completions requests."""

    def __init__(
        self, config: OpenAICompatibleConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        #: Cached prompt tokens the provider reported for the last dispatch. Kept off
        #: ``ModelUsage`` so the budget contract stays provider-neutral: this is an
        #: accounting fact, not a chargeable input.
        self.last_cache_read_tokens = 0
        if client is not None:
            self._client = client
            return
        # A desktop HTTP proxy must never intercept a loopback endpoint: the
        # proxy cannot reach 127.0.0.1 and answers 502. Remote endpoints keep
        # `trust_env` so corporate proxy configuration still applies. This
        # mirrors OMLXClient and the other local clients in this repository.
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds, trust_env=not _is_loopback(config.base_url)
        )

    async def decide(self, request: ModelRequest) -> ModelResult:
        """Request and validate exactly one final answer or tool call."""
        return await self.decide_prepared(self.prepare_request(request))

    def cache_plan(self, request: ModelRequest) -> CachePlan:
        """Expose the prefix plan so the runtime can record it in the trace.

        Deterministic in the request alone, so a replay recomputes the same plan and
        the recorded policy stays checkable against the recorded input.
        """
        return plan_cache(request, mode=self._config.cache_mode)

    def prepare_request(self, request: ModelRequest) -> PreparedModelCall:
        """Freeze the exact credential-free body that the transport will send."""
        body = self._request_body(request)
        explicit = self._config.cache_mode is CacheMode.EXPLICIT
        if explicit:
            body = apply_cache_plan(body, self.cache_plan(request))
        return PreparedModelCall(
            request_json=canonical_json(request.model_dump(mode="json")),
            payload_json=canonical_json(body),
            format="chat-completions-cache-explicit-v1" if explicit else "chat-completions-v1",
            output_limit=self._config.max_tokens,
            context_window=self._config.context_window_tokens,
            provider_overhead_tokens=self._config.provider_overhead_tokens,
        )

    async def decide_prepared(self, prepared: PreparedModelCall) -> ModelResult:
        """Send the admitted snapshot rather than regenerating its messages or schema."""
        if prepared.format not in {"chat-completions-v1", "chat-completions-cache-explicit-v1"}:
            raise ValueError("unexpected prepared request format")
        # httpx applies its timeout per network operation. A peer continuously
        # sending tiny chunks must also obey one total request/read deadline.
        async with asyncio.timeout(self._config.timeout_seconds):
            async with self._client.stream(
                "POST",
                f"{self._config.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                    # Refuse unsolicited compression below, before any inflation.
                    "Accept-Encoding": "identity",
                },
                content=prepared.payload_json.encode(),
                timeout=self._config.timeout_seconds,
                # A host-injected client's redirect loop must not buffer an
                # intermediate unbounded body before this response guard runs.
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                body = await self._read_response(response)
        try:
            completion = _ChatCompletion.model_validate(json.loads(body))
        except (ValidationError, ValueError) as exc:
            raise ModelProtocolError(f"invalid chat-completions response: {exc}") from exc
        if self._config.require_usage and (
            completion.usage is None
            or not {"prompt_tokens", "completion_tokens"} <= completion.usage.model_fields_set
        ):
            raise ModelProtocolError(
                "provider omitted usage", code="missing_usage", recoverable=False
            )
        usage = completion.usage or _RemoteUsage()
        self.last_cache_read_tokens = usage.cached_tokens
        if len(completion.choices) != 1:
            raise ModelProtocolError(
                f"expected exactly one completion choice, got {len(completion.choices)}"
            )
        remote_message = completion.choices[0].message
        if len(remote_message.tool_calls) > 1:
            # A recoverable model-protocol violation, surfaced as such so the
            # caller can decide how to handle it rather than losing the reason.
            raise ModelProtocolError(
                "parallel tool calls are not enabled for this runtime",
                code="multiple_tool_calls_not_allowed",
                received_tool_calls=len(remote_message.tool_calls),
                # The request consumed these tokens, so a run that retries must
                # still be charged for the rejected attempt.
                usage=ModelUsage(
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.completion_tokens,
                ),
            )
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
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            ),
        )

    async def _read_response(self, response: httpx.Response) -> bytes:
        """Close an oversized/encoded response without retaining its raw content."""
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise ModelProtocolError(
                "provider sent an unsupported encoded response",
                code="unsupported_content_encoding",
                recoverable=False,
            )
        limit = self._config.max_response_bytes
        length = response.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                raise ModelProtocolError(
                    "provider sent an invalid response length", recoverable=False
                ) from None
            if declared < 0:
                raise ModelProtocolError(
                    "provider sent an invalid response length", recoverable=False
                )
            if declared > limit:
                raise self._oversized_response()
        if response.is_stream_consumed:
            # Explicitly injected transports may return preloaded responses
            # (e.g. MockTransport). They are trusted host code; still refuse an
            # oversized document before copying/parsing it here.
            if len(response.content) > limit:
                raise self._oversized_response()
            return response.content
        captured = bytearray()
        async for chunk in response.aiter_raw(chunk_size=min(64 * 1024, limit + 1)):
            if len(captured) + len(chunk) > limit:
                raise self._oversized_response()
            captured.extend(chunk)
        return bytes(captured)

    @staticmethod
    def _oversized_response() -> ModelProtocolError:
        # No response prefix or untrusted header value is included in evidence.
        # The provider may have executed; missing trustworthy usage stays unknown.
        return ModelProtocolError(
            "provider response exceeds its byte limit",
            code="response_too_large",
            recoverable=False,
        )

    def _request_body(self, request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._config.model,
            "temperature": self._config.temperature,
            **chat_inputs(request),
        }
        if self._config.max_tokens is not None:
            body["max_tokens"] = self._config.max_tokens
        if self._config.enable_thinking is not None:
            if self._config.thinking_control == "thinking_object":
                mode = "enabled" if self._config.enable_thinking else "disabled"
                body["thinking"] = {"type": mode}
            else:
                body["chat_template_kwargs"] = {"enable_thinking": self._config.enable_thinking}
        return body

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        return chat_message(message)

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

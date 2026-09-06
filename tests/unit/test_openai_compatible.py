"""Tests for the OpenAI-compatible model boundary."""

import json

import httpx
import pytest

from forgeharness.domain.models import Message, MessageRole, ToolAction, ToolCall
from forgeharness.models.base import ModelRequest
from forgeharness.models.openai_compatible import (
    ModelProtocolError,
    OpenAICompatibleConfig,
    OpenAICompatibleModel,
)
from forgeharness.tools.builtin import EchoTool


def build_model(payload: dict[str, object], captured: list[httpx.Request]) -> OpenAICompatibleModel:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleModel(
        OpenAICompatibleConfig(
            base_url="https://model.example/v1/", api_key="secret", model="test-model"
        ),
        client=client,
    )


def build_bounded_model(
    payload: dict[str, object], captured: list[httpx.Request]
) -> OpenAICompatibleModel:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    return OpenAICompatibleModel(
        OpenAICompatibleConfig(
            base_url="http://127.0.0.1:8000/v1",
            api_key="local",
            model="local-model",
            max_tokens=128,
            enable_thinking=False,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def request(messages: tuple[Message, ...] | None = None) -> ModelRequest:
    return ModelRequest(
        task_id="task-1",
        messages=messages or (Message(role=MessageRole.USER, content="echo hello"),),
        tools=(EchoTool().spec,),
    )


async def test_adapter_parses_tool_call_and_usage() -> None:
    captured: list[httpx.Request] = []
    model = build_model(
        {
            "model": "served-model",
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
                                    "arguments": '{"text":"hello"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        },
        captured,
    )

    result = await model.decide(request())

    assert isinstance(result.action, ToolAction)
    assert result.action.call.arguments == {"text": "hello"}
    assert result.usage.input_tokens == 12
    assert result.model_name == "served-model"
    sent = json.loads(captured[0].content)
    assert sent["parallel_tool_calls"] is False
    assert sent["tools"][0]["function"]["name"] == "echo"
    assert captured[0].headers["Authorization"] == "Bearer secret"


async def test_adapter_sends_explicit_local_generation_bounds() -> None:
    captured: list[httpx.Request] = []
    model = build_bounded_model({"choices": [{"message": {"content": "complete"}}]}, captured)
    await model.decide(request())
    sent = json.loads(captured[0].content)
    assert sent["max_tokens"] == 128
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


async def test_adapter_serializes_assistant_and_tool_messages() -> None:
    captured: list[httpx.Request] = []
    model = build_model(
        {"choices": [{"message": {"content": "complete"}}]},
        captured,
    )
    call = ToolCall(id="call-1", name="echo", arguments={"text": "你好"})
    messages = (
        Message(role=MessageRole.USER, content="echo"),
        Message(role=MessageRole.ASSISTANT, tool_calls=(call,)),
        Message(
            role=MessageRole.TOOL,
            content="你好",
            tool_call_id="call-1",
            tool_name="echo",
        ),
    )

    result = await model.decide(request(messages))

    assert result.action.content == "complete"  # type: ignore[union-attr]
    sent = json.loads(captured[0].content)
    assert sent["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"text":"你好"}'
    assert sent["messages"][2]["tool_call_id"] == "call-1"


@pytest.mark.parametrize(
    ("message", "match"),
    [
        ({"content": None}, "neither content nor a tool call"),
        (
            {
                "tool_calls": [
                    {"id": "a", "function": {"name": "echo", "arguments": "{}"}},
                    {"id": "b", "function": {"name": "echo", "arguments": "{}"}},
                ]
            },
            "parallel tool calls",
        ),
        (
            {"tool_calls": [{"id": "a", "function": {"name": "echo", "arguments": "not-json"}}]},
            "not valid JSON",
        ),
        (
            {"tool_calls": [{"id": "a", "function": {"name": "echo", "arguments": "[]"}}]},
            "must decode to an object",
        ),
    ],
)
async def test_adapter_rejects_unsafe_response(message: dict[str, object], match: str) -> None:
    model = build_model({"choices": [{"message": message}]}, [])

    with pytest.raises(ModelProtocolError, match=match):
        await model.decide(request())


async def test_adapter_rejects_multiple_choices() -> None:
    model = build_model(
        {
            "choices": [
                {"message": {"content": "one"}},
                {"message": {"content": "two"}},
            ]
        },
        [],
    )

    with pytest.raises(ModelProtocolError, match="exactly one"):
        await model.decide(request())

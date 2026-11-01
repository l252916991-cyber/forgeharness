"""Untrusted HTTP responses are bounded before decoding, parsing or tracing."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from forgeharness.domain.models import Message, MessageRole
from forgeharness.models.base import ModelRequest
from forgeharness.models.openai_compatible import (
    ModelProtocolError,
    OpenAICompatibleConfig,
    OpenAICompatibleModel,
)


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def config(**kwargs):
    return OpenAICompatibleConfig(
        base_url="http://127.0.0.1:1234/v1",
        api_key="synthetic",
        model="synthetic",
        require_usage=True,
        max_response_bytes=1024,
        **kwargs,
    )


def request():
    return ModelRequest(
        task_id="response-bound",
        messages=(Message(role=MessageRole.USER, content="confirm"),),
        tools=(),
    )


@pytest.mark.parametrize("limit", [True, "1024", 1024.0, 0, 1023, 16 * 1024 * 1024 + 1])
def test_response_bound_is_an_explicit_strict_integer(limit):
    with pytest.raises(ValidationError):
        OpenAICompatibleConfig(
            base_url="http://127.0.0.1/v1", api_key="local", model="local", max_response_bytes=limit
        )


@pytest.mark.parametrize("timeout", [float("inf"), float("nan")])
def test_response_total_deadline_must_be_finite(timeout):
    with pytest.raises(ValidationError):
        config(timeout_seconds=timeout)


async def test_oversized_chunked_response_stops_reading_and_never_echoes_its_prefix():
    stream = TrackingStream([b"password=never-persist;" + b"x" * 490] * 100)

    async def handler(incoming):
        assert incoming.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleModel(config(), client=client)
        with pytest.raises(ModelProtocolError) as error:
            await model.decide(request())
    assert error.value.code == "response_too_large"
    assert error.value.usage is None
    assert error.value.recoverable is False
    assert "never-persist" not in str(error.value)
    assert stream.yielded <= 3
    assert stream.closed


@pytest.mark.parametrize("declared", ["1025", "99999999999999999999"])
async def test_oversized_declared_length_is_refused_before_consuming_any_body(declared):
    stream = TrackingStream([b"not read"])

    async def handler(incoming):
        return httpx.Response(200, headers={"Content-Length": declared}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelProtocolError) as error:
            await OpenAICompatibleModel(config(), client=client).decide(request())
    assert error.value.code == "response_too_large"
    assert stream.yielded == 0
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
async def test_compressed_body_is_refused_before_any_decompression(encoding):
    stream = TrackingStream([b"an untrusted compressed body"])

    async def handler(incoming):
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelProtocolError) as error:
            await OpenAICompatibleModel(config(), client=client).decide(request())
    assert error.value.code == "unsupported_content_encoding"
    assert error.value.usage is None
    assert stream.yielded == 0
    assert stream.closed


async def test_exact_response_limit_is_accepted_and_stream_is_closed():
    body = (
        json.dumps(
            {
                "choices": [{"message": {"content": "confirmed"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        )
        .encode()
        .ljust(1024, b" ")
    )
    stream = TrackingStream([body[:512], body[512:]])

    async def handler(incoming):
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleModel(config(), client=client).decide(request())
    assert result.action.content == "confirmed"
    assert result.usage.input_tokens == 3
    assert stream.yielded == 2
    assert stream.closed


async def test_preloaded_injected_transport_cannot_bypass_the_response_limit():
    async def handler(incoming):
        return httpx.Response(200, headers={"Content-Length": "1"}, content=b"x" * 2048)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelProtocolError) as error:
            await OpenAICompatibleModel(config(), client=client).decide(request())
    assert error.value.code == "response_too_large"


@pytest.mark.parametrize("length", ["negative", "-1"])
async def test_invalid_length_does_not_echo_an_untrusted_header(length):
    stream = TrackingStream([b"not read"])

    async def handler(incoming):
        return httpx.Response(200, headers={"Content-Length": length}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelProtocolError, match="invalid response length"):
            await OpenAICompatibleModel(config(), client=client).decide(request())
    assert stream.yielded == 0
    assert stream.closed


async def test_cancellation_while_streaming_closes_the_response():
    entered = asyncio.Event()

    class WaitingStream(TrackingStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    stream = WaitingStream([])

    async def handler(incoming):
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleModel(config(), client=client)
        task = asyncio.create_task(model.decide(request()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


async def test_injected_clients_cannot_follow_and_buffer_an_unbounded_redirect_body():
    stream = TrackingStream([b"not read"] * 100)
    seen = []

    async def handler(incoming):
        seen.append(str(incoming.url))
        return httpx.Response(
            302, headers={"Location": "http://unapproved.invalid/model"}, stream=stream
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await OpenAICompatibleModel(config(), client=client).decide(request())
    assert seen == ["http://127.0.0.1:1234/v1/chat/completions"]
    assert stream.yielded == 0
    assert stream.closed

"""Actual loopback model transport through PostgreSQL-hosted AgentRuntime.

The provider's wire ledger is independent of the Harness ledger. These tests use
real TCP, the public OpenAI-compatible adapter and no external model service.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from forgeharness.cloud.execution_contract import PhysicalResult
from forgeharness.cloud.hosted import HostedApplication
from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.domain.models import FinalAction, ModelResult, ModelUsage, Usage
from forgeharness.models.caching import CacheMode
from forgeharness.models.openai_compatible import OpenAICompatibleConfig, OpenAICompatibleModel
from forgeharness.models.prepared import canonical_json
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.verification import VerificationResult
from forgeharness.sdk.spec import ApplicationSpec

from .execution_fixtures import IndependentApprovalPolicy


@dataclass
class WireReceipt:
    request: dict[str, Any]
    request_bytes: bytes
    response: dict[str, Any]
    response_sent: bool = False


@dataclass
class LoopbackProvider:
    omit_usage: bool
    cache_mode: CacheMode = CacheMode.OFF
    response_padding: int = 0
    chunked_response: bool = False
    response_chunk_delay: float = 0
    response_chunk_size: int = 16 * 1024
    response_chunks_sent: int = 0
    receipts: list[WireReceipt] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    active: set[asyncio.Task] = field(default_factory=set)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = header.decode("ascii").split("\r\n")
            assert lines[0] == "POST /v1/chat/completions HTTP/1.1"
            headers = dict(line.split(": ", 1) for line in lines[1:] if line)
            length = int(
                next(value for key, value in headers.items() if key.lower() == "content-length")
            )
            assert 0 < length <= 64_000
            body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
            request = json.loads(body)
            receipt_id = "wire-" + hashlib.sha256(body).hexdigest()
            response = {
                "id": receipt_id,
                "model": "loopback-wire-v1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": canonical_json(
                                {"answer": "local provider confirmed", "receipt": receipt_id}
                            ),
                        }
                    }
                ],
            }
            if not self.omit_usage:
                response["usage"] = {
                    "prompt_tokens": 17,
                    "completion_tokens": 11,
                    "prompt_tokens_details": {
                        "cached_tokens": 0 if self.cache_mode is CacheMode.OFF else 7
                    },
                }
            if self.response_padding:
                response["untrusted_padding"] = (
                    "private-oversized-response;" + "x" * self.response_padding
                )
            receipt = WireReceipt(request=request, request_bytes=body, response=response)
            self.receipts.append(receipt)
            encoded = canonical_json(response).encode()
            header = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            if self.chunked_response:
                writer.write(header + b"Transfer-Encoding: chunked\r\n\r\n")
                for offset in range(0, len(encoded), self.response_chunk_size):
                    chunk = encoded[offset : offset + self.response_chunk_size]
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    await writer.drain()
                    self.response_chunks_sent += 1
                    if self.response_chunk_delay:
                        await asyncio.sleep(self.response_chunk_delay)
                writer.write(b"0\r\n\r\n")
                await writer.drain()
            else:
                writer.write(header + f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
                await writer.drain()
            receipt.response_sent = True
        except ConnectionError:
            if not self.response_padding:
                self.errors.append("ConnectionError")
        except Exception as exc:
            # Neither raw HTTP headers nor bearer credentials enter diagnostics.
            self.errors.append(type(exc).__name__)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.active.discard(task)

    def verified_result(self, physical) -> PhysicalResult | None:
        payload = physical.envelope.canonical_input["payload"]
        matches = [
            item
            for item in self.receipts
            if item.response_sent
            and item.request == payload
            and item.request_bytes == canonical_json(payload).encode()
        ]
        if len(matches) != 1:
            return None
        receipt = matches[0]
        usage = receipt.response.get("usage")
        if usage is None:
            # A delivered response proves execution, but cannot invent its cost.
            return None
        return PhysicalResult(
            kind="model",
            receipt_id=receipt.response["id"],
            actual_units=usage["prompt_tokens"] + usage["completion_tokens"],
            model_result=ModelResult(
                action=FinalAction(content=receipt.response["choices"][0]["message"]["content"]),
                model_name=receipt.response["model"],
                usage=ModelUsage(
                    input_tokens=usage["prompt_tokens"], output_tokens=usage["completion_tokens"]
                ),
            ),
        )


@asynccontextmanager
async def loopback_provider(
    *,
    omit_usage=False,
    cache_mode=CacheMode.OFF,
    response_padding=0,
    chunked_response=False,
    response_chunk_delay=0,
    response_chunk_size=16 * 1024,
    timeout_seconds=5,
):
    provider = LoopbackProvider(
        omit_usage=omit_usage,
        cache_mode=cache_mode,
        response_padding=response_padding,
        chunked_response=chunked_response,
        response_chunk_delay=response_chunk_delay,
        response_chunk_size=response_chunk_size,
    )
    server = await asyncio.start_server(provider.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            model = OpenAICompatibleModel(
                OpenAICompatibleConfig(
                    base_url=f"http://127.0.0.1:{port}/v1",
                    api_key="loopback-test-credential",
                    model="loopback-wire-v1",
                    require_usage=True,
                    max_tokens=32,
                    timeout_seconds=timeout_seconds,
                    cache_mode=cache_mode,
                ),
                client=client,
            )
            yield provider, model
    finally:
        server.close()
        await server.wait_closed()
        if provider.active:
            await asyncio.wait_for(asyncio.gather(*tuple(provider.active)), timeout=5)
        assert not provider.errors


class WireFinalVerifier:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0

    async def verify(self, request):
        self.calls += 1
        matching = [
            item
            for item in self.provider.receipts
            if item.response_sent
            and item.response["choices"][0]["message"]["content"] == request.final.content
        ]
        passed = len(matching) == 1 and matching[0].response.get("usage") is not None
        return VerificationResult(
            passed=passed,
            reason="independent loopback wire ledger checked",
            evidence=("loopback-wire://" + matching[0].response["id"],) if passed else (),
        )


def registration(provider, model, *, cache_mode=CacheMode.OFF):
    verifier = WireFinalVerifier(provider)

    async def verify_model(physical, observed):
        independently_verified = provider.verified_result(physical)
        if independently_verified is None or observed.result != independently_verified.model_result:
            return None
        return independently_verified

    async def verify_tool(_physical, _observed):
        raise AssertionError("the HTTP-only application has no tools")

    async def resolve(physical):
        return provider.verified_result(physical)

    spec = ApplicationSpec(
        id="loopback-http-app",
        version="1",
        policy_version="1",
        model_factory=lambda: model,
        tools=(),
        policy=IndependentApprovalPolicy(),
        verifier=verifier,
        instructions=("Return the local provider confirmation.",),
        budget=RunBudget(
            max_steps=2,
            max_tool_calls=0,
            max_input_tokens=2_000,
            max_output_tokens=100,
            reserve_output_tokens=32,
            max_attempts_per_call=1,
        ),
    )
    app = HostedApplication(
        application=spec,
        deployment_id="loopback-http",
        release_digest=hashlib.sha256(b"loopback-http-release-v1").hexdigest(),
        model_binding_digest=hashlib.sha256(
            canonical_json({"provider": "loopback-http-v1", "cache_mode": cache_mode}).encode()
        ).hexdigest(),
        max_run_units=500,
        model_reservation_units=100,
        tool_reservation_units={},
        model_verifier=verify_model,
        tool_verifier=verify_tool,
        resolver=resolve,
    )
    return app, verifier


@pytest.mark.parametrize("cache_mode", list(CacheMode))
async def test_real_pg_real_http_model_final_is_verified_charged_and_reconstructable(
    pgcase, cache_mode
):
    async with loopback_provider(cache_mode=cache_mode) as (provider, model):
        app, verifier = registration(provider, model, cache_mode=cache_mode)
        runtime = HostedRuntime(pgcase.store, {app.deployment_id: app})
        owner = await pgcase.seed("org-a", "owner", can_execute=False)
        worker = await pgcase.seed("org-a", "worker", can_execute=True)
        accepted = await runtime.accept(owner, app.deployment_id, "http-final", "Confirm locally.")
        final = await runtime.advance(worker, accepted.run.run_id)
        assert len(provider.receipts) == 1
        wire = provider.receipts[0]
        trace = await runtime.trace(owner, accepted.run.run_id)
        request = next(event for event in trace.events if event.type == "model.request")
        assert request.payload["prepared_payload"] == wire.request
        assert canonical_json(request.payload["prepared_payload"]).encode() == wire.request_bytes
        assert (
            request.payload["preflight"]["payload_sha256"]
            == hashlib.sha256(wire.request_bytes).hexdigest()
        )
        system_content = wire.request["messages"][0]["content"]
        if cache_mode is CacheMode.EXPLICIT:
            assert system_content == [
                {
                    "type": "text",
                    "text": "Return the local provider confirmation.",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            assert system_content == "Return the local provider confirmation."
        cache = next(event for event in trace.events if event.type == "model.cache")
        assert cache.payload["mode"] == cache_mode.value
        assert cache.payload["breakpoints"] == (1 if cache_mode is CacheMode.EXPLICIT else 0)
        observed = [event for event in trace.events if event.type == "model.cache.observed"]
        provider_cached = wire.response["usage"]["prompt_tokens_details"]["cached_tokens"]
        if provider_cached:
            assert len(observed) == 1
            assert observed[0].payload["cached_input_tokens"] == provider_cached
            assert observed[0].payload["input_tokens"] == wire.response["usage"]["prompt_tokens"]
        else:
            assert observed == []
        assert final.run.status == final.checkpoint.status == "succeeded"
        assert len(provider.receipts) == verifier.calls == 1
        assert wire.response_sent
        assert final.checkpoint.final_output == wire.response["choices"][0]["message"]["content"]
        assert final.checkpoint.usage == Usage(steps=1, input_tokens=17, output_tokens=11)
        assert final.run.used_units == 28
        assert final.run.reserved_units == 0
        action = next(event for event in trace.events if event.type == "model.action")
        assert action.payload["input_tokens"] == 17
        assert action.payload["output_tokens"] == 11
        assert action.payload["action"] == {
            "kind": "final",
            "content": wire.response["choices"][0]["message"]["content"],
        }
        assert trace.snapshot_pending() == ()
        assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.execution_calls") == 1
        assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.receipts") == 1
        assert (
            await pgcase.admin.fetchval(
                "SELECT count(*) FROM fh_cloud.invocations WHERE state='settled'"
            )
            == 1
        )
        await runtime.advance(worker, accepted.run.run_id)
        assert len(provider.receipts) == 1


async def test_real_pg_http_response_without_usage_stays_unknown_and_never_resends(pgcase):
    async with loopback_provider(omit_usage=True) as (provider, model):
        app, verifier = registration(provider, model)
        runtime = HostedRuntime(pgcase.store, {app.deployment_id: app})
        owner = await pgcase.seed("org-a", "owner", can_execute=False)
        worker = await pgcase.seed("org-a", "worker", can_execute=True)
        accepted = await runtime.accept(owner, app.deployment_id, "http-no-usage", "Confirm.")
        first = await runtime.advance(worker, accepted.run.run_id)
        assert first.run.status == "reconciling"
        assert first.checkpoint.status not in {"succeeded", "failed", "cancelled", "exhausted"}
        assert first.run.reserved_units == 100
        assert first.run.used_units == 0
        assert first.checkpoint.final_output is None
        assert len(provider.receipts) == 1
        assert provider.receipts[0].response_sent
        assert "usage" not in provider.receipts[0].response
        assert verifier.calls == 0
        other = await pgcase.seed("org-a", "other-worker", can_execute=True)
        repeated = await runtime.advance(other, accepted.run.run_id, worker_id="reconcile-only")
        assert repeated.run.status == "reconciling"
        assert repeated.run.reserved_units == 100
        assert repeated.run.used_units == 0
        assert len(provider.receipts) == 1
        assert verifier.calls == 0
        assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.receipts") == 0
        assert (
            await pgcase.admin.fetchval(
                "SELECT count(*) FROM fh_cloud.invocations WHERE state='unknown'"
            )
            == 1
        )
        trace = await runtime.trace(owner, accepted.run.run_id)
        failed = next(event for event in trace.events if event.type == "model.failed")
        assert failed.payload["error_code"] == "missing_usage"
        assert failed.payload["usage"] is None
        assert not any(event.type == "run.finished" for event in trace.events)
        assert trace.snapshot_pending() == ()


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
async def test_real_pg_oversized_real_http_response_holds_budget_and_never_resends(pgcase, chunked):
    async with loopback_provider(
        omit_usage=True, response_padding=8 * 1024 * 1024 + 1024, chunked_response=chunked
    ) as (provider, model):
        app, verifier = registration(provider, model)
        runtime = HostedRuntime(pgcase.store, {app.deployment_id: app})
        owner = await pgcase.seed("org-a", "owner", can_execute=False)
        worker = await pgcase.seed("org-a", "worker", can_execute=True)
        accepted = await runtime.accept(owner, app.deployment_id, "oversized-http", "Confirm.")
        first = await runtime.advance(worker, accepted.run.run_id)
        assert first.run.status == "reconciling"
        assert first.checkpoint.status not in {"succeeded", "failed", "cancelled", "exhausted"}
        assert first.checkpoint.final_output is None
        assert first.run.reserved_units == 100
        assert first.run.used_units == 0
        assert len(provider.receipts) == 1
        assert verifier.calls == 0
        second_worker = await pgcase.seed("org-a", "other-worker", can_execute=True)
        retried = await runtime.advance(second_worker, accepted.run.run_id, worker_id="reconcile")
        assert retried.run.status == "reconciling"
        assert retried.run.reserved_units == 100
        assert retried.run.used_units == 0
        assert len(provider.receipts) == 1
        assert verifier.calls == 0
        trace = await runtime.trace(owner, accepted.run.run_id)
        failed = next(event for event in trace.events if event.type == "model.failed")
        assert failed.payload["error_code"] == "response_too_large"
        assert failed.payload["recoverable"] is False
        assert failed.payload["usage"] is None
        assert "private-oversized-response" not in trace.export_json()
        assert "untrusted_padding" not in trace.export_json()
        assert len([event for event in trace.events if event.type == "model.request"]) == 1
        assert not any(event.type == "run.finished" for event in trace.events)
        assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.receipts") == 0
        assert (
            await pgcase.admin.fetchval(
                "SELECT count(*) FROM fh_cloud.invocations WHERE state='unknown'"
            )
            == 1
        )
        assert trace.snapshot_pending() == ()


async def test_real_pg_continuous_http_chunks_obey_total_deadline_without_resending(pgcase):
    async with loopback_provider(
        omit_usage=True,
        response_padding=20_000,
        chunked_response=True,
        response_chunk_size=64,
        response_chunk_delay=0.03,
        timeout_seconds=0.25,
    ) as (provider, model):
        app, verifier = registration(provider, model)
        runtime = HostedRuntime(pgcase.store, {app.deployment_id: app})
        owner = await pgcase.seed("org-a", "owner", can_execute=False)
        worker = await pgcase.seed("org-a", "worker", can_execute=True)
        accepted = await runtime.accept(owner, app.deployment_id, "slow-http", "Confirm.")
        # Each chunk arrives well within httpx's read timeout. Without a total
        # deadline this response would retain its connection for over 9 seconds.
        first = await asyncio.wait_for(runtime.advance(worker, accepted.run.run_id), timeout=3)
        assert provider.response_chunks_sent >= 2
        assert len(provider.receipts) == 1
        assert first.run.status == "reconciling"
        assert first.checkpoint.status not in {"succeeded", "failed", "cancelled", "exhausted"}
        assert first.run.reserved_units == 100
        assert first.run.used_units == 0
        assert first.checkpoint.final_output is None
        assert verifier.calls == 0
        other_worker = await pgcase.seed("org-a", "other-worker", can_execute=True)
        second = await runtime.advance(other_worker, accepted.run.run_id, worker_id="reconcile")
        assert second.run.status == "reconciling"
        assert second.run.reserved_units == 100
        assert second.run.used_units == 0
        assert len(provider.receipts) == 1
        assert verifier.calls == 0
        trace = await runtime.trace(owner, accepted.run.run_id)
        failed = next(event for event in trace.events if event.type == "model.failed")
        assert failed.payload["error_type"] == "TimeoutError"
        assert failed.payload["usage"] is None
        assert "private-oversized-response" not in trace.export_json()
        assert not any(event.type == "run.finished" for event in trace.events)
        assert len([event for event in trace.events if event.type == "model.request"]) == 1
        assert await pgcase.admin.fetchval("SELECT count(*) FROM fh_cloud.receipts") == 0
        assert trace.snapshot_pending() == ()

"""A live experiment stops after the provider refuses the account, not after the budget."""

from __future__ import annotations

import httpx
import pytest

from benchmarks.experiments.provider_guard import AccountRefused, ProviderGuard


async def _send(guard: ProviderGuard, statuses: list[int]) -> list[int]:
    replies = iter(statuses)
    sent: list[int] = []
    transport = httpx.MockTransport(lambda request: httpx.Response(next(replies)))
    async with httpx.AsyncClient(
        transport=transport, event_hooks={"response": [guard.on_response]}
    ) as client:
        for _ in statuses:
            # The driver's loop: check before every new unit of work.
            try:
                guard.check()
            except AccountRefused:
                break
            sent.append((await client.post("https://provider.invalid/chat")).status_code)
    return sent


async def test_the_run_stops_at_the_first_account_refusal() -> None:
    guard = ProviderGuard()
    assert await _send(guard, [200, 200, 402, 402, 402]) == [200, 200, 402]
    with pytest.raises(AccountRefused, match="HTTP 402"):
        guard.check()
    assert guard.summary() == {
        "stopped_early": True,
        "stop_reason": "provider refused the account (HTTP 402)",
    }


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_request_level_and_transient_errors_do_not_stop_the_run(status: int) -> None:
    guard = ProviderGuard()
    assert await _send(guard, [status, 200, 200]) == [status, 200, 200]
    guard.check()
    assert guard.summary() == {"stopped_early": False, "stop_reason": None}


async def test_the_first_refusal_is_the_one_recorded() -> None:
    guard = ProviderGuard()
    await guard.on_response(httpx.Response(401))
    await guard.on_response(httpx.Response(402))
    assert guard.refused_with == 401

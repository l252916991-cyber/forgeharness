"""Stop a live experiment as soon as the provider refuses the account itself.

The long-context study's first evaluation attempt kept sending requests for several
minutes after the balance ran out: every one came back HTTP 402, and only the summary
said the run was void. An HTTP 401 (bad key) or 402 (no balance) is not about one
request; every later request will be refused the same way. A runner that watches its
HTTP responses through this guard checks it before starting each new unit of work and
stops the whole run with a recorded reason instead of grinding through refusals.

Requests already in flight are left to finish: their refusals settle as failures
(ADR 050) and are kept as evidence. Finished studies keep their own runners unchanged,
because their sealed protocols pin those files by hash.
"""

from __future__ import annotations

import httpx

ACCOUNT_REFUSALS = frozenset({401, 402})


class AccountRefused(RuntimeError):
    """The provider refused the account; no further request can succeed."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider refused the account with HTTP {status_code}; run stopped")
        self.status_code = status_code


class ProviderGuard:
    """An httpx response hook plus a check the driver calls between units of work."""

    def __init__(self) -> None:
        self.refused_with: int | None = None

    async def on_response(self, response: httpx.Response) -> None:
        if response.status_code in ACCOUNT_REFUSALS and self.refused_with is None:
            self.refused_with = response.status_code

    def check(self) -> None:
        """Raise ``AccountRefused`` once any response has refused the account."""
        if self.refused_with is not None:
            raise AccountRefused(self.refused_with)

    def summary(self) -> dict[str, object]:
        """What a run summary records: whether and why the run was stopped early."""
        return {
            "stopped_early": self.refused_with is not None,
            "stop_reason": (
                None
                if self.refused_with is None
                else f"provider refused the account (HTTP {self.refused_with})"
            ),
        }

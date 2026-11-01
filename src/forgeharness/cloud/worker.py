"""Bounded serial PostgreSQL scanner; notifications are optional hints.

Construct with a trusted registry and explicitly provisioned worker context.
No HTTP route exposes this service. Graceful shutdown stops new claims, while an
already executing call is allowed to complete; abrupt loss uses the lease fence.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from time import monotonic

from forgeharness.cloud.models import CloudConflict, CloudDenied, TenantContext
from forgeharness.cloud.runtime import HostedRuntime


@dataclass
class WorkerMetrics:
    scans: int = 0
    advanced: int = 0
    idle: int = 0
    conflicts: int = 0
    failures: int = 0


class HostedWorker:
    def __init__(
        self,
        runtime: HostedRuntime,
        context: TenantContext,
        *,
        worker_id: str,
        poll_interval: float = 0.25,
        max_backoff: float = 5,
        lease_seconds: float = 30,
    ) -> None:
        for value in (poll_interval, max_backoff, lease_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("worker timings must be positive and finite")
        if max_backoff < poll_interval or lease_seconds > 3600:
            raise ValueError("invalid worker timing bounds")
        self.runtime, self.context, self.worker_id = runtime, context, worker_id
        self.poll_interval, self.max_backoff = poll_interval, max_backoff
        self.lease_seconds = lease_seconds
        self.metrics = WorkerMetrics()
        self._retry: dict[str, tuple[float, float]] = {}
        self._tick_lock = asyncio.Lock()

    async def tick(self, *, _stop: asyncio.Event | None = None) -> int:
        """Advance at most one eligible durable run; return a progress count."""
        async with self._tick_lock:
            async with self.runtime.store.transaction(
                self.context,
                "execute",
                allow_inactive_org=True,
            ) as connection:
                # This worker serves only the platform-bound runs in its explicit
                # organization; it cannot silently adopt schema-2 host workloads.
                rows = await connection.fetch(
                    "SELECT r.run_id,r.status,c.revision FROM fh_cloud.runs r "
                    "JOIN fh_cloud.run_bindings b USING(org_id,run_id) "
                    "JOIN fh_cloud.execution_checkpoints c USING(org_id,run_id) "
                    "WHERE r.org_id=$1 AND r.status IN "
                    "('queued','running','awaiting_approval','reconciling') "
                    "AND (r.lease_until IS NULL OR r.lease_until<=clock_timestamp()) "
                    "ORDER BY r.created_at,r.run_id LIMIT 100",
                    self.context.org_id,
                )
            self.metrics.scans += 1
            active_ids = {row["run_id"] for row in rows}
            self._retry = {key: val for key, val in self._retry.items() if key in active_ids}
            for row in rows:
                if _stop is not None and _stop.is_set():
                    break
                run_id = row["run_id"]
                retry_at, delay = self._retry.get(run_id, (0.0, self.poll_interval / 2))
                if retry_at > monotonic():
                    continue
                changed = False
                try:
                    view = await self.runtime.advance(
                        self.context,
                        run_id,
                        worker_id=self.worker_id,
                        ttl_seconds=self.lease_seconds,
                    )
                    changed = (
                        view.run.status != row["status"]
                        or view.checkpoint.checkpoint_revision != row["revision"]
                    )
                except CloudConflict:
                    self.metrics.conflicts += 1
                except CloudDenied:
                    # A revoked owner is converged by runtime on the next claim.
                    # A revoked service itself fails the next scanner transaction.
                    self.metrics.failures += 1
                except Exception:
                    # No arbitrary exception text, DSN or task is logged. The
                    # supervisor sees metrics and may inspect protected evidence.
                    self.metrics.failures += 1
                if changed:
                    self._retry.pop(run_id, None)
                    self.metrics.advanced += 1
                    return 1
                delay = min(self.max_backoff, max(self.poll_interval, delay * 2))
                self._retry[run_id] = (monotonic() + delay, delay)
            self.metrics.idle += 1
            return 0

    async def run(self, stop: asyncio.Event) -> None:
        delay = self.poll_interval
        while not stop.is_set():
            try:
                progressed = await self.tick(_stop=stop)
                delay = self.poll_interval if progressed else min(self.max_backoff, delay * 2)
            except (CloudConflict, CloudDenied):
                self.metrics.failures += 1
                delay = min(self.max_backoff, delay * 2)
            except Exception:
                self.metrics.failures += 1
                delay = min(self.max_backoff, delay * 2)
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

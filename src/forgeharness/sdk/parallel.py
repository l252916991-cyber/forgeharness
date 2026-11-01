"""Local, host-owned provider admission; workers never own run state."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import stat
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from forgeharness.domain.models import FrozenModel
from forgeharness.sdk.privacy import CredentialMask
from forgeharness.sdk.spec import ScopeError
from forgeharness.state.sqlite import connect_wal


class ParallelExecution(FrozenModel):
    """Trusted host policy, included in durable identity; no model can set it."""

    max_in_flight: Literal[1, 2] = 1
    model_concurrency_safe: bool = Field(default=False, strict=True)
    request_timeout_seconds: float = Field(default=120, gt=0, le=3600, allow_inf_nan=False)
    drain_timeout_seconds: float = Field(default=5, gt=0, le=300, allow_inf_nan=False)
    poll_interval_seconds: float = Field(default=0.05, gt=0, le=1, allow_inf_nan=False)

    @field_validator("max_in_flight", mode="before")
    @classmethod
    def integer_limit(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("model concurrency must be an integer, not a boolean or string")
        return value

    @model_validator(mode="after")
    def safe_adapter(self) -> ParallelExecution:
        if self.max_in_flight > 1 and not self.model_concurrency_safe:
            raise ValueError("parallel calls require a host-declared concurrency-safe model")
        return self


class CapacityError(RuntimeError):
    """The provider domain is closed, saturated or owned by another process."""


@dataclass(frozen=True)
class CapacityTicket:
    invocation_id: str
    run_id: str
    node_id: str

    def __post_init__(self) -> None:
        for value in (self.invocation_id, self.run_id, self.node_id):
            if not isinstance(value, str) or not re.fullmatch(
                r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,199}", value
            ):
                raise ValueError("capacity ticket requires bounded registered identities")
            if CredentialMask().transform(value) != value:
                raise ValueError("capacity ticket must not contain credentials")


@dataclass
class _Waiter:
    ticket: CapacityTicket
    future: asyncio.Future[CapacityTicket]


class ModelDispatcher:
    """One event-loop/process per configured provider capacity domain.

    Use as a synchronous context manager surrounding all participating Harness
    calls. Unknown sent claims survive close/restart. Clearing them requires an
    authoritative receipt or a privileged host assertion that the backend is idle.
    The same provider must use the same private directory in every host process.
    """

    def __init__(
        self, *, state_dir: Path, provider_id: str, capacity: int = 2, max_queue: int = 64
    ) -> None:
        if (
            not isinstance(provider_id, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", provider_id)
            or CredentialMask().transform(provider_id) != provider_id
        ):
            raise ValueError("provider_id must be a bounded credential-free registered identity")
        if type(capacity) is not int or capacity not in {1, 2}:
            raise ValueError("provider capacity must be 1 or 2")
        if type(max_queue) is not int or not 1 <= max_queue <= 1000:
            raise ValueError("provider queue must be between 1 and 1000")
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.state_dir.stat().st_mode) & 0o077:
            raise ScopeError("provider state directory must be private (0700)")
        self.provider_id, self.capacity, self.max_queue = provider_id, capacity, max_queue
        self._pid = os.getpid()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: OrderedDict[str, deque[_Waiter]] = OrderedDict()
        self._active: dict[str, CapacityTicket] = {}
        self._unresolved: set[str] = set()
        self._closed = False
        self._workers: set[asyncio.Task[Any]] = set()
        stem = hashlib.sha256(provider_id.encode()).hexdigest()
        self.database = self.state_dir / f"{stem}.sqlite"
        self._lock_path = self.state_dir / f"{stem}.lock"
        if self._lock_path.is_symlink() or self.database.is_symlink():
            raise ScopeError("provider state files cannot be symbolic links")
        self._lock = self._lock_path.open("a+b")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise CapacityError("provider domain already has a live dispatcher") from exc
        try:
            with connect_wal(self.database) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS claims "
                    "(invocation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL, "
                    "stage TEXT NOT NULL, evidence TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS capacity_events "
                    "(sequence INTEGER PRIMARY KEY AUTOINCREMENT, invocation_id TEXT NOT NULL, "
                    "event TEXT NOT NULL, observed_at TEXT NOT NULL, evidence TEXT NOT NULL)"
                )
                # An unsent claim cannot have reached a provider: sent is durable
                # before the run marker and before worker creation.
                connection.execute("UPDATE claims SET stage='ended' WHERE stage='reserved'")
                rows = connection.execute(
                    "SELECT invocation_id,run_id,node_id FROM claims WHERE stage='sent'"
                ).fetchall()
            for invocation, run, node in rows:
                self._active[invocation] = CapacityTicket(invocation, run, node)
                self._unresolved.add(invocation)
        except BaseException:
            self._lock.close()
            raise

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "version": "local-provider-capacity-v1",
            "provider_id": self.provider_id,
            "state_dir": str(self.state_dir),
            "capacity": self.capacity,
            "max_queue": self.max_queue,
        }

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(sorted(self._unresolved))

    def _check(self) -> None:
        if self._closed or os.getpid() != self._pid:
            raise CapacityError("dispatcher is closed or belongs to another process")
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if self._loop is not loop:
            raise CapacityError("dispatcher cannot span event loops or threads")

    def request(self, ticket: CapacityTicket) -> asyncio.Future[CapacityTicket]:
        self._check()
        if self._unresolved:
            raise CapacityError("provider capacity requires reconciliation")
        if ticket.invocation_id in self._active or any(
            w.ticket.invocation_id == ticket.invocation_id
            for queue in self._queues.values()
            for w in queue
        ):
            raise CapacityError("invocation already owns or awaits provider capacity")
        if sum(len(q) for q in self._queues.values()) >= self.max_queue:
            raise CapacityError("provider queue is full")
        future: asyncio.Future[CapacityTicket] = asyncio.get_running_loop().create_future()
        self._queues.setdefault(ticket.run_id, deque()).append(_Waiter(ticket, future))
        self._drain()
        return future

    def _record(self, ticket: CapacityTicket, stage: str, evidence: tuple[str, ...] = ()) -> None:
        with connect_wal(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT run_id,node_id FROM claims WHERE invocation_id=?", (ticket.invocation_id,)
            ).fetchone()
            if previous is not None and previous != (ticket.run_id, ticket.node_id):
                raise CapacityError("capacity invocation identity cannot change")
            connection.execute(
                "INSERT INTO claims VALUES(?,?,?,?,?) ON CONFLICT(invocation_id) DO UPDATE SET "
                "stage=excluded.stage,evidence=excluded.evidence",
                (ticket.invocation_id, ticket.run_id, ticket.node_id, stage, json.dumps(evidence)),
            )
            connection.execute(
                "INSERT INTO capacity_events(invocation_id,event,observed_at,evidence) "
                "VALUES(?,?,?,?)",
                (ticket.invocation_id, stage, datetime.now(UTC).isoformat(), json.dumps(evidence)),
            )

    def _drain(self) -> None:
        if self._unresolved:
            for queue in self._queues.values():
                for waiter in queue:
                    if not waiter.future.done():
                        waiter.future.set_exception(
                            CapacityError("provider capacity requires reconciliation")
                        )
            self._queues.clear()
            return
        while self._queues and not self._unresolved and len(self._active) < self.capacity:
            run, queue = self._queues.popitem(last=False)
            waiter = queue.popleft()
            if queue:
                self._queues[run] = queue
            if waiter.future.cancelled():
                continue
            try:
                self._record(waiter.ticket, "reserved")
            except Exception as exc:
                waiter.future.set_exception(exc)
                continue
            self._active[waiter.ticket.invocation_id] = waiter.ticket
            waiter.future.set_result(waiter.ticket)

    def withdraw(self, ticket: CapacityTicket) -> None:
        """Cancel a queue entry or release a proven-unsent capacity grant."""
        self._check()
        for run, queue in tuple(self._queues.items()):
            kept = deque(w for w in queue if w.ticket != ticket)
            for waiter in queue:
                if waiter.ticket == ticket:
                    waiter.future.cancel()
            if kept:
                self._queues[run] = kept
            else:
                del self._queues[run]
        if ticket.invocation_id in self._active:
            with connect_wal(self.database) as connection:
                row = connection.execute(
                    "SELECT stage FROM claims WHERE invocation_id=?", (ticket.invocation_id,)
                ).fetchone()
            if row is not None and row[0] == "reserved":
                self.finish(ticket, confirmed=True)
        self._drain()

    def sent(self, ticket: CapacityTicket) -> None:
        self._check()
        if self._unresolved:
            raise CapacityError("provider capacity requires reconciliation")
        if self._active.get(ticket.invocation_id) != ticket:
            raise CapacityError("sending requires owned capacity")
        self._record(ticket, "sent")

    def finish(self, ticket: CapacityTicket, *, confirmed: bool) -> None:
        self._check()
        if self._active.get(ticket.invocation_id) != ticket:
            with connect_wal(self.database) as connection:
                ended = connection.execute(
                    "SELECT run_id,node_id,stage FROM claims WHERE invocation_id=?",
                    (ticket.invocation_id,),
                ).fetchone()
            if confirmed and ended == (ticket.run_id, ticket.node_id, "ended"):
                return
            raise CapacityError("capacity result does not match an active ticket")
        if confirmed:
            self._record(ticket, "ended")
            del self._active[ticket.invocation_id]
            self._unresolved.discard(ticket.invocation_id)
        else:
            # Do not release a slot merely because an HTTP waiter disappeared.
            self._record(ticket, "sent")
            self._unresolved.add(ticket.invocation_id)
        self._drain()

    def watch_worker(self, task: asyncio.Task[Any]) -> None:
        """Keep ownership while a noncooperative adapter still runs locally."""
        self._check()
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    def confirm_ended(self, invocation_id: str, *, evidence_refs: tuple[str, ...]) -> None:
        """Privileged host receipt assertion; never exposed as a model tool."""
        self._check()
        if (
            not evidence_refs
            or any(
                not isinstance(ref, str)
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,199}", ref)
                for ref in evidence_refs
            )
            or CredentialMask().transform(evidence_refs) != list(evidence_refs)
        ):
            raise ValueError("capacity reconciliation requires bounded receipt references")
        if invocation_id not in self._unresolved:
            raise CapacityError("only unresolved capacity can be reconciled")
        ticket = self._active[invocation_id]
        self._record(ticket, "ended", evidence_refs)
        del self._active[invocation_id]
        self._unresolved.remove(invocation_id)
        self._drain()

    def close(self) -> None:
        if self._closed:
            return
        if os.getpid() != self._pid:
            raise CapacityError("cannot close another process's dispatcher")
        if set(self._active) - self._unresolved:
            raise CapacityError("finish active calls before closing the dispatcher")
        if any(not task.done() for task in self._workers):
            raise CapacityError("provider adapter has not stopped its local worker")
        for queue in self._queues.values():
            for waiter in queue:
                waiter.future.cancel()
        self._queues.clear()
        self._closed = True
        self._lock.close()

    def __enter__(self) -> ModelDispatcher:
        if self._closed:
            raise CapacityError("dispatcher is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

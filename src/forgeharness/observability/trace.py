"""Append-only trace protocol and in-memory implementation."""

from __future__ import annotations

from typing import Any, Protocol

from forgeharness.domain.models import TraceEvent


class TraceRecorder(Protocol):
    """Append sanitized, model-visible execution evidence."""

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> TraceEvent:
        """Append and return one sequence-addressed event."""
        ...


class InMemoryTrace:
    """Record trace events for tests and local one-shot runs."""

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self._events: list[TraceEvent] = []

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> TraceEvent:
        """Append one event with a monotonic sequence number."""
        event = TraceEvent(
            task_id=self._task_id,
            sequence=len(self._events) + 1,
            type=event_type,
            payload=payload or {},
        )
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """Return an immutable view of recorded events."""
        return tuple(self._events)

"""Redacted JSONL traces with a tamper-evident SHA-256 chain."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from forgeharness.domain.models import FrozenModel, TraceEvent

_SENSITIVE_KEY = re.compile(
    r"(?i)(^|[_-])(api[_-]?key|authorization|password|secret|access[_-]?token|"
    r"refresh[_-]?token|bearer)([_-]|$)"
)
_SENSITIVE_VALUE = re.compile(r"(?i)\b(bearer\s+\S+|sk-[a-z0-9_-]{8,})")


class TraceEnvelope(FrozenModel):
    """One event and the hashes required to verify its position in the chain."""

    event: TraceEvent
    previous_hash: str = Field(pattern=r"^(|[a-f0-9]{64})$")
    hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TraceVerification(FrozenModel):
    """Result of checking every line and chain link in a trace file."""

    valid: bool
    events: int = Field(ge=0)
    error: str | None = None


class HashChainedJSONLTrace:
    """Append fsynced, redacted events and expose the TraceRecorder interface."""

    def __init__(self, path: Path, task_id: str) -> None:
        self._path = path
        self._task_id = task_id
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and self._path.stat().st_size:
            verification = verify_trace(self._path)
            if not verification.valid:
                raise ValueError(f"cannot append to invalid trace: {verification.error}")
            envelopes = _load_envelopes(self._path)
            if any(envelope.event.task_id != task_id for envelope in envelopes):
                raise ValueError("trace task id does not match")
            self._sequence = len(envelopes)
            self._previous_hash = envelopes[-1].hash
        else:
            self._sequence = 0
            self._previous_hash = ""

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> TraceEvent:
        """Redact recursively, append one event, and fsync it before returning."""
        event = TraceEvent(
            task_id=self._task_id,
            sequence=self._sequence + 1,
            type=event_type,
            payload=_redact(payload or {}),
        )
        event_json = _canonical_event(event)
        digest = hashlib.sha256((self._previous_hash + event_json).encode()).hexdigest()
        envelope = TraceEnvelope(event=event, previous_hash=self._previous_hash, hash=digest)
        with self._path.open("a", encoding="utf-8") as file:
            file.write(envelope.model_dump_json() + "\n")
            file.flush()
            os.fsync(file.fileno())
        self._sequence += 1
        self._previous_hash = digest
        return event


def verify_trace(path: Path) -> TraceVerification:
    """Verify JSON structure, sequence numbers, and every hash link."""
    try:
        envelopes = _load_envelopes(path)
    except (OSError, ValueError) as exc:
        return TraceVerification(valid=False, events=0, error=f"invalid trace JSON: {exc}")
    previous = ""
    for index, envelope in enumerate(envelopes, start=1):
        if envelope.event.sequence != index:
            return TraceVerification(valid=False, events=index - 1, error="invalid event sequence")
        if envelope.previous_hash != previous:
            return TraceVerification(valid=False, events=index - 1, error="broken previous hash")
        expected = hashlib.sha256(
            (previous + _canonical_event(envelope.event)).encode()
        ).hexdigest()
        if envelope.hash != expected:
            return TraceVerification(valid=False, events=index - 1, error="event hash mismatch")
        previous = envelope.hash
    return TraceVerification(valid=True, events=len(envelopes))


def _load_envelopes(path: Path) -> list[TraceEnvelope]:
    return [
        TraceEnvelope.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _canonical_event(event: TraceEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _redact(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("[REDACTED]", value)
    return value

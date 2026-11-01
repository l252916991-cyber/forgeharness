"""Bounded, synchronous staging for the existing hash-chained trace protocol.

This buffer performs no I/O and provides no durability guarantee. A host exports
``snapshot_pending()`` before its PostgreSQL checkpoint transaction, awaits a
successful commit, and only then calls ``ack_committed()`` with that exact
snapshot. Failure leaves the snapshot pending. ``pending()``/``ack()`` remain
available for bounded transport batches that do not commit a full checkpoint.
The complete bounded history is retained so its chain remains independently
verifiable; reaching a limit rejects the next append rather than dropping facts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from threading import RLock
from typing import Any

from forgeharness.domain.models import TraceEvent
from forgeharness.observability.hash_chain import TraceEnvelope, _canonical_event
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform

_HASH = re.compile(r"^(|[a-f0-9]{64})$")
_MAX_EVENTS = 10_000
_MAX_BYTES = 16 * 1024 * 1024
_MAX_EVENT_BYTES = 256 * 1024
_MAX_DEPTH = 32
_MAX_NODES = 100_000


def _limit(value: int, ceiling: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= ceiling:
        raise ValueError(f"invalid evidence {name}")
    return value


def _copy_json(value: Any) -> Any:
    """Reject coercions, cycles/deep trees, nonfinite numbers and invalid Unicode."""
    nodes = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("evidence JSON structure exceeds limits")
        kind = type(item)
        if item is None or kind is bool or kind is int:
            return item
        if kind is float:
            if not math.isfinite(item):
                raise ValueError("evidence numbers must be finite")
            return item
        if kind is str:
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise ValueError("invalid evidence Unicode") from None
            return item
        if kind is list:
            return [visit(child, depth + 1) for child in item]
        if kind is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("evidence object keys must be strings")
            return {visit(key, depth + 1): visit(child, depth + 1) for key, child in item.items()}
        raise ValueError("evidence must contain only JSON values")

    return visit(value, 0)


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, OverflowError):
        raise ValueError("invalid evidence JSON") from None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence JSON key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    del value
    raise ValueError("evidence numbers must be finite")


def _credential_safe(value: Any) -> bool:
    return bool(CredentialMask().transform(value) == value)


def _event(**values: Any) -> TraceEvent:
    try:
        return TraceEvent(**values)
    except ValueError:
        # Validation errors must not echo untrusted metadata into host logs.
        raise ValueError("invalid evidence event metadata") from None


class BufferedEvidence:
    """Stage compatible ``TraceEnvelope`` JSON without claiming it is persisted.

    New payloads pass through the host's EvidenceTransform and credential masking.
    Returned events are copies; changing their nested dictionaries cannot change
    the staged bytes. Acknowledgement advances only an exact pending prefix and
    remains safe when new events arrive while the host awaits a database commit.
    All limits bound the complete retained history, including acknowledged events.
    """

    def __init__(
        self,
        task_id: str,
        *,
        transform: EvidenceTransform | None = None,
        max_events: int = _MAX_EVENTS,
        max_bytes: int = _MAX_BYTES,
        max_event_bytes: int = _MAX_EVENT_BYTES,
        batch_size: int = 100,
    ) -> None:
        if type(task_id) is not str or not _credential_safe(task_id):
            raise ValueError("invalid evidence task identity")
        _event(task_id=task_id, sequence=1, type="run.started")
        self._task_id = task_id
        self._transform = transform if transform is not None else CredentialMask()
        self._max_events = _limit(max_events, _MAX_EVENTS, "event limit")
        self._max_bytes = _limit(max_bytes, _MAX_BYTES, "byte limit")
        if self._max_bytes < 2:
            raise ValueError("invalid evidence byte limit")
        self._max_event_bytes = _limit(max_event_bytes, _MAX_EVENT_BYTES, "event byte limit")
        self._batch_size = _limit(batch_size, _MAX_EVENTS, "batch size")
        self._entries: list[str] = []
        self._acknowledged = 0
        self._bytes = 2  # Canonical JSON array delimiters, including when empty.
        self._head = ""
        self._lock = RLock()

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> TraceEvent:
        """Transform, validate and stage an event; no file or database is written."""
        if type(event_type) is not str or not _credential_safe(event_type):
            raise ValueError("invalid evidence event type")
        raw = _copy_json({} if payload is None else payload)
        if type(raw) is not dict:
            raise ValueError("evidence payload must be an object")
        if len(_json(raw).encode("utf-8")) > self._max_event_bytes:
            raise ValueError("evidence event exceeds byte limit")
        try:
            transformed = self._transform.transform(raw)
        except Exception:
            raise ValueError("evidence transformation failed") from None
        # Credential masking is mandatory even for a custom domain transform.
        sanitized = _copy_json(CredentialMask().transform(_copy_json(transformed)))
        if type(sanitized) is not dict:
            raise ValueError("evidence transform must return an object")
        with self._lock:
            event = _event(
                task_id=self._task_id,
                sequence=len(self._entries) + 1,
                type=event_type,
                payload=sanitized,
            )
            digest = hashlib.sha256((self._head + _canonical_event(event)).encode()).hexdigest()
            envelope = TraceEnvelope(event=event, previous_hash=self._head, hash=digest)
            encoded = _json(envelope.model_dump(mode="json"))
            self._retain(encoded, digest)
            return event.model_copy(deep=True)

    def _retain(self, encoded: str, digest: str) -> None:
        size = len(encoded.encode("utf-8"))
        added = size + bool(self._entries)
        if size > self._max_event_bytes:
            raise ValueError("evidence event exceeds byte limit")
        if len(self._entries) >= self._max_events or self._bytes + added > self._max_bytes:
            raise ValueError("evidence buffer exceeds limits")
        self._entries.append(encoded)
        self._bytes += added
        self._head = digest

    def pending(self, *, limit: int | None = None) -> tuple[str, ...]:
        """Return a bounded immutable batch of canonical TraceEnvelope JSON."""
        count = (
            self._batch_size if limit is None else _limit(limit, self._batch_size, "batch limit")
        )
        with self._lock:
            return tuple(self._entries[self._acknowledged : self._acknowledged + count])

    def snapshot_pending(self) -> tuple[str, ...]:
        """Snapshot all pending evidence for one atomic checkpoint transaction.

        Unlike a transport batch, this includes every pending event, so a committed
        checkpoint cannot reference evidence left out by the transport batch limit.
        The complete snapshot remains bounded by this buffer's history limits.
        """
        with self._lock:
            return tuple(self._entries[self._acknowledged :])

    def ack(self, batch: tuple[str, ...]) -> None:
        """Acknowledge an exact pending prefix only after its durable commit."""
        self._ack_prefix(batch, self._batch_size)

    def ack_committed(self, batch: tuple[str, ...]) -> None:
        """Confirm an exact snapshot prefix after the whole PG transaction commits.

        Events appended after ``snapshot_pending()`` stay pending. This operation
        accepts the bounded full snapshot, without the transport batch-size limit.
        """
        self._ack_prefix(batch, self._max_events)

    def _ack_prefix(self, batch: tuple[str, ...], max_count: int) -> None:
        if type(batch) is not tuple or any(type(entry) is not str for entry in batch):
            raise ValueError("evidence acknowledgement must be an exported batch")
        if len(batch) > max_count:
            raise ValueError("evidence acknowledgement exceeds batch limit")
        with self._lock:
            prefix = tuple(self._entries[self._acknowledged : self._acknowledged + len(batch)])
            if prefix != batch:
                raise ValueError("evidence acknowledgement does not match pending prefix")
            self._acknowledged += len(batch)

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """Return independent event values for existing trace projections."""
        with self._lock:
            return tuple(TraceEnvelope.model_validate_json(entry).event for entry in self._entries)

    def export_json(self) -> str:
        """Export the complete compatible envelope array, without a commit claim."""
        with self._lock:
            return "[" + ",".join(self._entries) + "]"

    @classmethod
    def from_json(
        cls,
        task_id: str,
        serialized: str,
        *,
        transform: EvidenceTransform | None = None,
        max_events: int = _MAX_EVENTS,
        max_bytes: int = _MAX_BYTES,
        max_event_bytes: int = _MAX_EVENT_BYTES,
        batch_size: int = 100,
        expected_head: str | None = None,
    ) -> BufferedEvidence:
        """Verify and restore full history, initially treating every event as pending.

        Restore never reapplies a possibly non-idempotent domain transformation to
        historical events. It rejects unmasked credentials and checks the existing
        trace protocol. A trusted externally stored ``expected_head`` additionally
        detects truncation; a hash chain alone is not authentication. The host must
        explicitly acknowledge any prefix it has confirmed in durable storage.
        """
        buffer = cls(
            task_id,
            transform=transform,
            max_events=max_events,
            max_bytes=max_bytes,
            max_event_bytes=max_event_bytes,
            batch_size=batch_size,
        )
        if expected_head is not None and (
            type(expected_head) is not str or _HASH.fullmatch(expected_head) is None
        ):
            raise ValueError("invalid expected evidence head")
        if type(serialized) is not str:
            raise ValueError("evidence history must be JSON text")
        try:
            if len(serialized.encode("utf-8")) > max_bytes:
                raise ValueError("evidence history exceeds byte limit")
            decoded = json.loads(serialized, object_pairs_hook=_pairs, parse_constant=_constant)
        except (UnicodeError, RecursionError, json.JSONDecodeError):
            raise ValueError("invalid evidence JSON") from None
        if type(decoded) is not list or len(decoded) > max_events:
            raise ValueError("invalid evidence history or event limit")
        for index, value in enumerate(decoded, 1):
            buffer._restore_envelope(value, index)
        if expected_head is not None and buffer._head != expected_head:
            raise ValueError("evidence head does not match trusted history")
        return buffer

    def _restore_envelope(self, value: Any, index: int) -> None:
        if type(value) is not dict or set(value) != {"event", "previous_hash", "hash"}:
            raise ValueError("invalid evidence envelope")
        data = value["event"]
        fields = {"task_id", "sequence", "type", "event_version", "timestamp", "payload"}
        if type(data) is not dict or set(data) != fields:
            raise ValueError("invalid evidence event")
        # Payload limits are per event, just as they are on append. Applying the
        # tree depth/node limit to the outer history would reject valid long
        # histories or a payload at the permitted maximum depth on restoration.
        _copy_json(data["payload"])
        if (
            type(data["sequence"]) is not int
            or data["sequence"] != index
            or type(data["event_version"]) is not int
            or data["event_version"] != 1
            or type(data["timestamp"]) is not str
            or type(data["payload"]) is not dict
            or type(data["task_id"]) is not str
            or type(data["type"]) is not str
            or type(value["previous_hash"]) is not str
            or type(value["hash"]) is not str
            or data["task_id"] != self._task_id
            or not _credential_safe(data)
        ):
            raise ValueError("invalid evidence event identity, sequence or credentials")
        try:
            envelope = TraceEnvelope.model_validate(value)
        except ValueError:
            raise ValueError("invalid evidence envelope metadata") from None
        if envelope.event.timestamp.utcoffset() is None:
            raise ValueError("evidence timestamp needs a timezone")
        digest = hashlib.sha256(
            (self._head + _canonical_event(envelope.event)).encode()
        ).hexdigest()
        if envelope.previous_hash != self._head or envelope.hash != digest:
            raise ValueError("evidence hash chain does not match")
        self._retain(_json(envelope.model_dump(mode="json")), digest)

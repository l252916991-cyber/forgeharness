"""Bounded trace staging: persistence is acknowledged by the host, never implied."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from forgeharness.cloud.evidence import BufferedEvidence
from forgeharness.domain.models import TraceEvent
from forgeharness.observability.hash_chain import TraceEnvelope, _canonical_event


def _history(payload=None):
    """Produce the existing observability envelope format independently of the buffer."""
    event = TraceEvent(
        task_id="run-1",
        sequence=1,
        type="tool.completed",
        timestamp=datetime(2030, 1, 1, tzinfo=UTC),
        payload=payload or {},
    )
    digest = hashlib.sha256(_canonical_event(event).encode()).hexdigest()
    envelope = TraceEnvelope(event=event, previous_hash="", hash=digest)
    return [envelope.model_dump(mode="json")]


def test_failed_commit_preserves_exact_batch_until_successful_acknowledgement():
    trace = BufferedEvidence("run-1")
    trace.append("run.started", {"input": "synthetic"})
    batch = trace.pending()

    def failed_transaction():
        raise RuntimeError("synthetic commit failure")

    with pytest.raises(RuntimeError):
        failed_transaction()
    assert trace.pending() == batch
    assert len(trace.events) == 1
    trace.ack(batch)
    assert trace.pending() == ()
    # Acknowledgement keeps the complete chain available for verification.
    assert len(json.loads(trace.export_json())) == 1


def test_ack_does_not_discard_events_added_while_commit_was_in_flight():
    trace = BufferedEvidence("run-1", batch_size=2)
    trace.append("run.started")
    before_commit = trace.pending()
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(trace.append, "tool.completed", {"output": "new"}).result()
    trace.ack(before_commit)
    remaining = trace.pending()
    assert len(remaining) == 1
    assert json.loads(remaining[0])["event"]["sequence"] == 2
    trace.ack(remaining)
    assert trace.pending() == ()


def test_parallel_appends_keep_a_contiguous_valid_chain():
    trace = BufferedEvidence("run-1")
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda number: trace.append("tool.completed", {"n": number}), range(30)))
    restored = BufferedEvidence.from_json("run-1", trace.export_json())
    assert [event.sequence for event in restored.events] == list(range(1, 31))
    assert {event.payload["n"] for event in restored.events} == set(range(30))


def test_ack_requires_exact_prefix_content_and_cannot_be_replayed():
    trace = BufferedEvidence("run-1")
    trace.append("run.started")
    trace.append("tool.completed", {"result": 1})
    batch = trace.pending()
    for invalid in (batch[::-1], (batch[0] + " ",), (batch[1],), batch + batch):
        with pytest.raises(ValueError, match="prefix"):
            trace.ack(invalid)
        assert trace.pending() == batch
    trace.ack(batch[:1])
    with pytest.raises(ValueError, match="prefix"):
        trace.ack(batch[:1])
    assert trace.pending() == batch[1:]
    with pytest.raises(ValueError, match="exported batch"):
        trace.ack(list(batch))


def test_batches_are_bounded_and_total_history_limit_never_drops_evidence():
    trace = BufferedEvidence("run-1", max_events=3, batch_size=2)
    for _ in range(3):
        trace.append("run.started")
    assert len(trace.pending()) == 2
    with pytest.raises(ValueError, match="batch limit"):
        trace.pending(limit=3)
    trace.ack(trace.pending())
    retained = trace.export_json()
    with pytest.raises(ValueError, match="buffer exceeds"):
        trace.append("tool.completed")
    assert trace.export_json() == retained
    assert len(trace.pending()) == 1


def test_returned_values_and_original_payload_cannot_mutate_staged_evidence():
    trace = BufferedEvidence("run-1")
    payload = {"nested": {"values": [1, 2]}}
    event = trace.append("model.request", payload)
    expected = trace.export_json()
    payload["nested"]["values"].append(3)
    event.payload["nested"]["values"].append(4)
    trace.events[0].payload["nested"]["values"].append(5)
    assert trace.export_json() == expected


def test_credential_mask_and_custom_transform_run_before_export():
    class DomainTransform:
        def __init__(self):
            self.calls = 0

        def transform(self, value):
            self.calls += 1
            return {"safe": value["safe"], "person": "[PRIVATE]", "token": "raw-secret"}

    transform = DomainTransform()
    trace = BufferedEvidence("run-1", transform=transform)
    event = trace.append("model.request", {"safe": "yes", "person": "synthetic-name"})
    assert transform.calls == 1
    assert event.payload == {"safe": "yes", "person": "[PRIVATE]", "token": "[REDACTED]"}
    assert "synthetic-name" not in trace.export_json()
    assert "raw-secret" not in trace.export_json()
    BufferedEvidence.from_json("run-1", trace.export_json(), transform=transform)
    assert transform.calls == 1  # A non-idempotent domain transform is not reapplied.


def test_default_transform_masks_nested_and_free_text_credentials():
    trace = BufferedEvidence("run-1")
    trace.append(
        "model.request",
        {
            "nested": {"authorization": "Bearer raw-credential", "safe": "kept"},
            "message": "got ghp_abcdefgh1234 and sk-abcdefgh1234; password=abc",
            "input_tokens": 42,
        },
    )
    value = trace.export_json()
    assert "raw-credential" not in value
    assert "ghp_abcdefgh1234" not in value
    assert "sk-abcdefgh1234" not in value
    assert "password=abc" not in value
    assert trace.events[0].payload["input_tokens"] == 42
    assert trace.events[0].payload["nested"]["safe"] == "kept"


def test_restore_uses_the_existing_trace_envelope_and_resets_commit_claims():
    history = _history({"result": "synthetic"})
    serialized = json.dumps(history)
    trace = BufferedEvidence.from_json("run-1", serialized, expected_head=history[-1]["hash"])
    assert trace.events[0].type == "tool.completed"
    first = trace.pending()
    trace.ack(first)
    exported = trace.export_json()
    restored = BufferedEvidence.from_json("run-1", exported)
    assert restored.pending() == first
    restored.append("run.finished", {"status": "succeeded"})
    second = json.loads(restored.pending()[1])
    assert second["previous_hash"] == history[0]["hash"]
    assert second["event"]["sequence"] == 2
    assert TraceEnvelope.model_validate(second).event.event_version == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["event"]["payload"].update(result="changed"),
        lambda item: item.update(previous_hash="a" * 64),
        lambda item: item.update(hash="b" * 64),
        lambda item: item["event"].update(sequence=2),
        lambda item: item["event"].update(sequence=True),
        lambda item: item["event"].update(event_version=True),
        lambda item: item["event"].update(event_version=2),
        lambda item: item["event"].update(task_id="other"),
        lambda item: item["event"].update(timestamp="2030-01-01T00:00:00"),
        lambda item: item["event"].update(unexpected="field"),
        lambda item: item.update(unexpected="field"),
    ],
)
def test_restore_rejects_tampering_and_ambiguous_event_fields(mutation):
    history = _history({"result": "original"})
    mutation(history[0])
    with pytest.raises(ValueError):
        BufferedEvidence.from_json("run-1", json.dumps(history))


def test_restore_rejects_unmasked_secret_even_with_a_recomputed_valid_hash():
    serialized = json.dumps(_history({"api_key": "sk-abcdefgh1234"}))
    with pytest.raises(ValueError, match="credentials"):
        BufferedEvidence.from_json("run-1", serialized)


def test_trusted_head_detects_removed_tail_and_empty_history():
    trace = BufferedEvidence("run-1")
    trace.append("run.started")
    trace.append("run.finished")
    history = json.loads(trace.export_json())
    head = history[-1]["hash"]
    for truncated in (history[:1], []):
        with pytest.raises(ValueError, match="trusted history"):
            BufferedEvidence.from_json("run-1", json.dumps(truncated), expected_head=head)
    assert BufferedEvidence.from_json("run-1", "[]", expected_head="").events == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": -float("inf")},
        {"value": object()},
        {"value": {"set"}},
        {"value": (1, 2)},
        {1: "key"},
        {"value": b"bytes"},
        {"value": "\ud800"},
        [],
        False,
    ],
)
def test_append_rejects_non_json_values_without_consuming_a_sequence(payload):
    trace = BufferedEvidence("run-1")
    with pytest.raises(ValueError):
        trace.append("model.request", payload)
    assert trace.events == ()
    assert trace.append("run.started").sequence == 1


def test_cyclic_and_deep_payloads_are_rejected_before_transform():
    trace = BufferedEvidence("run-1")
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="structure"):
        trace.append("model.request", cyclic)
    deep = {}
    for _ in range(35):
        deep = {"child": deep}
    with pytest.raises(ValueError, match="structure"):
        trace.append("model.request", deep)


@pytest.mark.parametrize(
    "serialized",
    [
        "{}",
        "null",
        "[NaN]",
        '[{"x":1,"x":2}]',
        "[",
        "[Infinity]",
    ],
)
def test_restore_rejects_invalid_json_and_duplicate_keys(serialized):
    with pytest.raises(ValueError):
        BufferedEvidence.from_json("run-1", serialized)


def test_event_and_history_byte_limits_fail_without_mutating_the_chain():
    trace = BufferedEvidence("run-1", max_event_bytes=500)
    with pytest.raises(ValueError, match="event exceeds"):
        trace.append("model.request", {"content": "x" * 500})
    trace.append("run.started")
    original = trace.export_json()
    with pytest.raises(ValueError, match="event exceeds"):
        trace.append("model.request", {"content": "x" * 400})
    assert trace.export_json() == original
    with pytest.raises(ValueError, match="byte limit"):
        BufferedEvidence.from_json("run-1", original, max_bytes=len(original.encode()) - 1)
    limited = BufferedEvidence("run-1", max_bytes=len(original.encode()) + 32)
    limited.append("run.started")
    with pytest.raises(ValueError, match="buffer exceeds"):
        limited.append("run.finished")


@pytest.mark.parametrize(
    "limits",
    [
        {"max_events": True},
        {"max_events": 0},
        {"max_events": 10_001},
        {"max_bytes": 0},
        {"max_bytes": 1},
        {"max_event_bytes": 0},
        {"batch_size": 0},
    ],
)
def test_configuration_is_strict_and_bounded(limits):
    with pytest.raises(ValueError):
        BufferedEvidence("run-1", **limits)


def test_invalid_transform_output_and_failure_do_not_advance_the_buffer():
    class BrokenTransform:
        def transform(self, value):
            del value
            raise RuntimeError("untrusted credentials should not appear in the error")

    trace = BufferedEvidence("run-1", transform=BrokenTransform())
    with pytest.raises(ValueError, match=r"^evidence transformation failed$"):
        trace.append("run.started")
    assert trace.events == ()

    class InvalidTransform:
        def transform(self, value):
            del value
            return {"value": float("nan")}

    with pytest.raises(ValueError, match="finite"):
        BufferedEvidence("run-1", transform=InvalidTransform()).append("run.started")


def test_restore_preserves_payload_at_the_same_per_event_depth_limit():
    payload = {"leaf": "synthetic"}
    for _ in range(30):
        payload = {"child": payload}
    trace = BufferedEvidence("run-1")
    trace.append("model.request", payload)
    assert BufferedEvidence.from_json("run-1", trace.export_json()).events == trace.events


def test_restore_checks_each_event_limit_and_total_event_count():
    trace = BufferedEvidence("run-1")
    trace.append("run.started")
    trace.append("tool.completed", {"text": "x" * 100})
    with pytest.raises(ValueError, match="event limit"):
        BufferedEvidence.from_json("run-1", trace.export_json(), max_events=1)
    with pytest.raises(ValueError, match="event exceeds"):
        BufferedEvidence.from_json("run-1", trace.export_json(), max_event_bytes=200)


def test_restore_rejects_json_numeric_overflow_and_escaped_invalid_unicode():
    history = json.dumps(_history({"result": "replace-me"}))
    for invalid in ("1e999", '"\\ud800"'):
        with pytest.raises(ValueError):
            BufferedEvidence.from_json("run-1", history.replace('"replace-me"', invalid))


@pytest.mark.parametrize("field,value", [("sequence", "1"), ("type", 1), ("task_id", 1)])
def test_restore_rejects_metadata_type_coercion(field, value):
    history = _history()
    history[0]["event"][field] = value
    with pytest.raises(ValueError):
        BufferedEvidence.from_json("run-1", json.dumps(history))


def test_transform_must_preserve_a_json_object_root():
    class InvalidTransform:
        def transform(self, value):
            del value
            return []

    with pytest.raises(ValueError, match="return an object"):
        BufferedEvidence("run-1", transform=InvalidTransform()).append("run.started")


def test_identity_and_expected_head_cannot_smuggle_credentials_or_invalid_types():
    with pytest.raises(ValueError, match="task identity"):
        BufferedEvidence("sk-abcdefgh1234")
    trace = BufferedEvidence("run-1")
    with pytest.raises(ValueError, match="event type"):
        trace.append("sk-abcdefgh1234")
    with pytest.raises(ValueError, match="expected evidence head"):
        BufferedEvidence.from_json("run-1", "[]", expected_head=True)
    with pytest.raises(ValueError, match="JSON text"):
        BufferedEvidence.from_json("run-1", b"[]")


def test_metadata_validation_errors_do_not_echo_untrusted_values():
    with pytest.raises(ValueError, match=r"^invalid evidence event metadata$"):
        BufferedEvidence("invalid identity")
    trace = BufferedEvidence("run-1")
    with pytest.raises(ValueError, match=r"^invalid evidence event metadata$"):
        trace.append("invalid event type")
    history = _history()
    history[0]["event"]["timestamp"] = "untrusted-invalid-timestamp"
    with pytest.raises(ValueError, match=r"^invalid evidence envelope metadata$"):
        BufferedEvidence.from_json("run-1", json.dumps(history))
    assert trace.events == ()


def test_ack_cannot_exceed_the_configured_export_batch():
    trace = BufferedEvidence("run-1", batch_size=1)
    trace.append("run.started")
    batch = trace.pending()
    with pytest.raises(ValueError, match="acknowledgement exceeds"):
        trace.ack(batch + batch)
    assert trace.pending() == batch


def test_node_count_and_unserializable_integer_fail_before_staging():
    trace = BufferedEvidence("run-1")
    with pytest.raises(ValueError, match="structure"):
        trace.append("model.request", {"values": [None] * 100_001})
    with pytest.raises(ValueError, match="invalid evidence JSON"):
        trace.append("model.request", {"value": 10**5000})
    assert trace.events == ()


def test_restore_rejects_invalid_unicode_and_invalid_hash_metadata():
    with pytest.raises(ValueError, match="invalid evidence JSON"):
        BufferedEvidence.from_json("run-1", "\ud800")
    history = _history()
    history[0]["hash"] = "bad-hash"
    with pytest.raises(ValueError, match="invalid evidence envelope metadata"):
        BufferedEvidence.from_json("run-1", json.dumps(history))
    with pytest.raises(ValueError, match="expected evidence head"):
        BufferedEvidence.from_json("run-1", "[]", expected_head="bad-hash")


def test_complete_snapshot_exceeds_transport_batch_and_commits_atomically():
    trace = BufferedEvidence("run-1")
    for number in range(151):
        trace.append("tool.completed", {"n": number})
    assert len(trace.pending()) == 100
    snapshot = trace.snapshot_pending()
    assert type(snapshot) is tuple
    assert len(snapshot) == 151
    with pytest.raises(ValueError, match="acknowledgement exceeds"):
        trace.ack(snapshot)
    assert trace.snapshot_pending() == snapshot
    trace.ack_committed(snapshot)
    assert trace.pending() == trace.snapshot_pending() == ()
    assert len(trace.events) == 151


def test_ack_committed_retains_concurrent_appends_after_the_complete_snapshot():
    trace = BufferedEvidence("run-1")
    for number in range(125):
        trace.append("tool.completed", {"n": number})
    in_flight = trace.snapshot_pending()
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(trace.append, "run.finished", {"status": "succeeded"}).result()
    trace.ack_committed(in_flight)
    remaining = trace.snapshot_pending()
    assert len(remaining) == 1
    assert json.loads(remaining[0])["event"]["sequence"] == 126
    assert trace.pending() == remaining
    trace.ack_committed(remaining)
    assert trace.snapshot_pending() == ()


def test_ack_committed_rejects_reordering_changed_content_and_duplicate_confirmation():
    trace = BufferedEvidence("run-1")
    for number in range(121):
        trace.append("tool.completed", {"n": number})
    snapshot = trace.snapshot_pending()
    for invalid in (snapshot[::-1], snapshot[1:], (snapshot[0] + " ", *snapshot[1:])):
        with pytest.raises(ValueError, match="pending prefix"):
            trace.ack_committed(invalid)
        assert trace.snapshot_pending() == snapshot
    trace.ack_committed(snapshot[:110])
    assert trace.snapshot_pending() == snapshot[110:]
    with pytest.raises(ValueError, match="pending prefix"):
        trace.ack_committed(snapshot[:110])
    assert trace.snapshot_pending() == snapshot[110:]
    trace.ack_committed(snapshot[110:])
    with pytest.raises(ValueError, match="pending prefix"):
        trace.ack_committed(snapshot)

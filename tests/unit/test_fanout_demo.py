"""The fan-out example: bounded governed groups, merged in code, failures reported."""

from __future__ import annotations

import json
from pathlib import Path

from examples.fanout_demo import GROUP_SIZE, REFUSED_RECORD, fan_out, records


async def test_groups_merge_and_a_refused_group_is_reported_not_dropped(tmp_path: Path) -> None:
    summary = await fan_out(tmp_path)
    keys = list(records())
    refused = next(
        i for i in range(0, len(keys), GROUP_SIZE) if REFUSED_RECORD in keys[i : i + GROUP_SIZE]
    )
    unchecked = keys[refused : refused + GROUP_SIZE]
    expected = sorted(
        key
        for key, text in records().items()
        if key not in unchecked and int(text.rsplit("value=", 1)[1]) > 50
    )
    assert summary["groups"] == len(keys) // GROUP_SIZE
    assert summary["failed_groups"] == [refused // GROUP_SIZE + 1]
    assert summary["unchecked_records"] == unchecked
    assert summary["violations"] == expected and summary["traces_valid"] is True


def _events(group: Path) -> list[dict]:  # type: ignore[type-arg]
    return [
        json.loads(line)["event"]
        for trace in group.rglob("*.jsonl")
        for line in trace.read_text().splitlines()
    ]


async def test_the_refusal_is_a_settled_failure_in_the_groups_trace(tmp_path: Path) -> None:
    await fan_out(tmp_path)
    events = _events(tmp_path / "group-3")
    failed = [e["payload"] for e in events if e["type"] == "model.failed"]
    assert failed and failed[0]["error_code"] == "provider_rejected"
    assert failed[0]["status_code"] == 402

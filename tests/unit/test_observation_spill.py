"""Tests for observation spill and read-back.

Compression used to be the end of the road: the projection replaced the original and
the bytes were gone. These tests pin the replacement behaviour — the original is
written before the projection, the reference in the header resolves back to it, and a
coding run can page it back without re-running the tool that produced it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from forgeharness.coding.tools import (
    ListFilesInput,
    ListFilesTool,
    ReadObservationInput,
    ReadObservationTool,
)
from forgeharness.context.compression import (
    ObservationCompressor,
    SpillCorrupt,
    SpillNotFound,
    SpillUnavailable,
    observation_spill_dir,
)
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.tools.base import RiskLevel, ToolContext, ToolOutput, ToolSpec
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry

_REFERENCE = re.compile(r"obs://[0-9a-f]{24}")


def long_text(lines: int = 400) -> str:
    """Build an observation that always exceeds the smallest permitted ceiling."""
    return "\n".join(f"step {index}: processed target {index}" for index in range(lines))


def compressor(tmp_path: Path, *, max_chars: int = 400) -> ObservationCompressor:
    return ObservationCompressor(max_chars=max_chars, spill_dir=observation_spill_dir(tmp_path))


def reference_from(text: str) -> str:
    match = _REFERENCE.search(text)
    assert match is not None, text[:200]
    return match.group(0)


# --------------------------------------------------------------------------- #
# Spill and read-back
# --------------------------------------------------------------------------- #


def test_short_content_is_returned_unchanged_and_writes_nothing(tmp_path: Path) -> None:
    result = compressor(tmp_path).compress_detailed("ok")

    assert result.text == "ok"
    assert result.reference is None
    assert not observation_spill_dir(tmp_path).exists()


def test_long_content_is_projected_but_the_original_is_kept(tmp_path: Path) -> None:
    content = long_text()
    result = compressor(tmp_path).compress_detailed(content)

    assert result.compressed
    assert result.reference is not None
    assert f"full: {result.reference}" in result.text
    assert len(result.text) <= 400
    stored = observation_spill_dir(tmp_path) / f"{result.reference.removeprefix('obs://')}.txt"
    assert stored.read_text(encoding="utf-8") == content


def test_read_back_returns_the_exact_original_bytes(tmp_path: Path) -> None:
    content = "unicode: 中文 ✓\n" * 200
    subject = compressor(tmp_path)
    reference = subject.compress_detailed(content).reference
    assert reference is not None

    assert subject.read_spilled(reference) == content


def test_identical_observations_share_one_stored_file(tmp_path: Path) -> None:
    content = long_text()
    subject = compressor(tmp_path)

    first = subject.compress_detailed(content)
    second = subject.compress_detailed(content)

    assert first.reference == second.reference
    assert len(list(observation_spill_dir(tmp_path).glob("*.txt"))) == 1


def test_paging_reads_one_window_without_pulling_the_whole_observation(tmp_path: Path) -> None:
    content = long_text()
    subject = compressor(tmp_path)
    reference = subject.compress_detailed(content).reference
    assert reference is not None

    window = subject.read_spilled(reference, offset=10, limit=25)

    assert window == content[10:35]


def test_an_unknown_reference_is_reported_as_missing(tmp_path: Path) -> None:
    with pytest.raises(SpillNotFound):
        compressor(tmp_path).read_spilled("obs://" + "0" * 24)


def test_a_reference_that_is_not_one_of_ours_is_rejected(tmp_path: Path) -> None:
    subject = compressor(tmp_path)

    for bogus in ("/etc/passwd", "obs://../../etc/passwd", "obs://ABCDEF", "obs://"):
        with pytest.raises(ValueError, match="not an observation reference"):
            subject.read_spilled(bogus)


def test_a_tampered_file_is_reported_as_corrupt(tmp_path: Path) -> None:
    subject = compressor(tmp_path)
    reference = subject.compress_detailed(long_text()).reference
    assert reference is not None
    stored = observation_spill_dir(tmp_path) / f"{reference.removeprefix('obs://')}.txt"
    stored.write_text("rewritten", encoding="utf-8")

    with pytest.raises(SpillCorrupt):
        subject.read_spilled(reference)


def test_a_compressor_without_a_spill_directory_reports_it(tmp_path: Path) -> None:
    subject = ObservationCompressor(max_chars=400)
    result = subject.compress_detailed(long_text())

    assert result.reference is None
    assert result.text.startswith("[compressed observation:")
    with pytest.raises(SpillUnavailable):
        subject.read_spilled("obs://" + "0" * 24)


def test_projection_is_deterministic_across_instances(tmp_path: Path) -> None:
    content = long_text()

    first = ObservationCompressor(max_chars=400).compress(content)
    second = ObservationCompressor(max_chars=400).compress(content)

    assert first == second


def test_spill_directory_is_workspace_scoped(tmp_path: Path) -> None:
    assert observation_spill_dir(tmp_path) == tmp_path / ".forgeharness" / "observations"

    subject = ObservationCompressor.for_workspace(tmp_path)

    assert subject.spill_dir == observation_spill_dir(tmp_path)


def test_binding_the_directory_late_matches_binding_it_up_front(tmp_path: Path) -> None:
    content = long_text()
    late = ObservationCompressor(max_chars=400)
    late.bind_spill_dir(observation_spill_dir(tmp_path))

    assert late.compress(content) == compressor(tmp_path).compress(content)


# --------------------------------------------------------------------------- #
# The read_observation tool
# --------------------------------------------------------------------------- #


async def test_read_observation_tool_serves_a_stored_reference(tmp_path: Path) -> None:
    content = long_text()
    reference = compressor(tmp_path).compress_detailed(content).reference
    assert reference is not None

    result = await ReadObservationTool().execute(
        ReadObservationInput(reference=reference),
        ToolContext(task_id="task", workspace=tmp_path),
    )

    assert result.ok
    assert result.content == content
    assert result.metadata["returned_chars"] == len(content)


async def test_read_observation_tool_reports_a_bad_reference_as_an_observation(
    tmp_path: Path,
) -> None:
    result = await ReadObservationTool().execute(
        ReadObservationInput(reference="obs://" + "f" * 24),
        ToolContext(task_id="task", workspace=tmp_path),
    )

    assert not result.ok
    assert "cannot read obs://" in result.content
    assert result.metadata["error_code"] == "SpillNotFound"


async def test_harness_state_is_not_listed_as_repository_content(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    spill = observation_spill_dir(tmp_path)
    spill.mkdir(parents=True)
    (spill / "deadbeef.txt").write_text("noise")

    listing = await ListFilesTool().execute(
        ListFilesInput(), ToolContext(task_id="task", workspace=tmp_path)
    )

    assert listing.content.splitlines() == ["src/app.py"]


# --------------------------------------------------------------------------- #
# Runtime wiring
# --------------------------------------------------------------------------- #


class BulkInput(BaseModel):
    """No-argument input for the bulk tool."""


class BulkTool:
    """Return an observation long enough to be compressed."""

    payload = long_text(600)

    input_model = BulkInput
    spec = ToolSpec(
        name="bulk",
        description="Return a long observation.",
        input_schema=BulkInput.model_json_schema(),
        risk=RiskLevel.READ,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        return ToolOutput(ok=True, content=self.payload)


def build_runtime(model: ScriptedModel, *, spill: bool) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(BulkTool())
    return AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=InMemoryTrace("task-1"),
        observation_compressor=ObservationCompressor(max_chars=400),
        observation_spill=spill,
    )


def bulk_run() -> ScriptedModel:
    return ScriptedModel(
        [
            ModelResult(
                action=ToolAction(call=ToolCall(id="call-1", name="bulk", arguments={})),
                model_name="scripted",
            ),
            ModelResult(action=FinalAction(content="done"), model_name="scripted"),
        ]
    )


async def test_runtime_spills_a_compressed_observation_into_the_workspace(
    tmp_path: Path,
) -> None:
    model = bulk_run()

    result = await build_runtime(model, spill=True).run(
        task_id="task-1", task="bulk", workspace=tmp_path
    )

    assert result.status is RunStatus.SUCCEEDED
    projection = model.requests[1].messages[-1].content
    reference = reference_from(projection)
    stored = observation_spill_dir(tmp_path) / f"{reference.removeprefix('obs://')}.txt"
    assert stored.read_text(encoding="utf-8") == BulkTool.payload


async def test_runtime_leaves_the_workspace_clean_when_spill_is_off(tmp_path: Path) -> None:
    model = bulk_run()

    await build_runtime(model, spill=False).run(task_id="task-1", task="bulk", workspace=tmp_path)

    projection = model.requests[1].messages[-1].content
    assert "full: obs://" not in projection
    assert not observation_spill_dir(tmp_path).exists()

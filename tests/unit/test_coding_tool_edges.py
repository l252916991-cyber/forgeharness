"""Edge branches in the coding tools, the cache planner, the loader and the adapter.

The happy path for each of these is covered elsewhere; what is left are the branches a
refusal takes: no matches, a truncated output, a bad reference, a manifest that parses
to the wrong shape. They are the branches that decide what a caller sees when something
is wrong, so they deserve their own assertions rather than a shared "works" test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeharness.coding.tools import (
    GitDiffTool,
    ReadCompactedContextInput,
    ReadCompactedContextTool,
    RunTestsTool,
    SearchCodeInput,
    SearchCodeTool,
    WriteFileInput,
    WriteFileTool,
)
from forgeharness.context.spill import MessageSpill
from forgeharness.domain.models import Message, MessageRole
from forgeharness.models.base import ModelRequest
from forgeharness.models.caching import CacheMode, plan_cache
from forgeharness.models.openai_compatible import OpenAICompatibleConfig, OpenAICompatibleModel
from forgeharness.models.prepared import prepare_model_call
from forgeharness.skills.loader import SkillLoader, SkillLoadError, SkillRegistry
from forgeharness.tools.base import ExecutionAllowance, ToolContext
from forgeharness.tools.process import CommandResult


def context(workspace: Path) -> ToolContext:
    return ToolContext(task_id="task", workspace=workspace)


def result(output: str = "", exit_code: int = 0, truncated: bool = False) -> CommandResult:
    return CommandResult(exit_code=exit_code, output=output, truncated=truncated)


class StubBackend:
    """A backend that answers from a script, so each refusal branch is reachable."""

    name = "stub"

    def __init__(self, *, search: CommandResult | None = None, run: CommandResult | None = None):
        self._search = search or result()
        self._run = run or result()
        self.commands: list[tuple[str, ...]] = []

    def list_files(self, **_kwargs: object) -> tuple[()]:
        return ()

    def read_text(self, *_args: object, **_kwargs: object) -> object:
        raise NotImplementedError

    def write_text(self, *_args: object, **_kwargs: object) -> object:
        raise NotImplementedError

    def digest_of(self, _path: str) -> str:
        raise NotImplementedError

    async def search(self, _query: str, **_kwargs: object) -> CommandResult:
        return self._search

    async def run(self, command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        self.commands.append(command)
        return self._run


def factory(backend: StubBackend) -> object:
    return lambda _root: backend


# ------------------------------------------------------------------------- search


async def test_search_without_matches_says_so(tmp_path: Path) -> None:
    backend = StubBackend(search=result("", exit_code=1))
    output = await SearchCodeTool(backend=factory(backend)).execute(  # type: ignore[arg-type]
        SearchCodeInput(query="needle"), context(tmp_path)
    )

    # ripgrep exits 1 when nothing matches; that is an answer, not a failure.
    assert output.ok is True
    assert output.content == "no matches"


async def test_search_reports_a_real_failure(tmp_path: Path) -> None:
    backend = StubBackend(search=result("rg: bad regex", exit_code=2))
    output = await SearchCodeTool(backend=factory(backend)).execute(  # type: ignore[arg-type]
        SearchCodeInput(query="["), context(tmp_path)
    )

    assert output.ok is False
    assert output.content == "rg: bad regex"


async def test_search_marks_a_truncated_output(tmp_path: Path) -> None:
    backend = StubBackend(search=result("hit", truncated=True))
    output = await SearchCodeTool(backend=factory(backend)).execute(  # type: ignore[arg-type]
        SearchCodeInput(query="hit"), context(tmp_path)
    )

    assert output.content.endswith("[output truncated]")


# ---------------------------------------------------------------------- run/git


async def test_a_failing_test_command_is_a_failed_tool_call(tmp_path: Path) -> None:
    backend = StubBackend(run=result("1 failed", exit_code=1, truncated=True))
    output = await RunTestsTool(command=("pytest", "-q"), backend=factory(backend)).execute(  # type: ignore[arg-type]
        {}, context(tmp_path)
    )

    assert output.ok is False
    assert output.content.endswith("[output truncated]")
    assert output.metadata["exit_code"] == 1


async def test_git_diff_runs_the_fixed_argument_vector(tmp_path: Path) -> None:
    backend = StubBackend(run=result("diff --git a/x b/x", truncated=True))
    output = await GitDiffTool(backend=factory(backend)).execute({}, context(tmp_path))  # type: ignore[arg-type]

    assert backend.commands == [("git", "diff", "--no-ext-diff", "--")]
    assert output.content.endswith("[output truncated]")
    assert output.ok is True


# --------------------------------------------------------------------- write


async def test_writing_over_a_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    output = await WriteFileTool().execute(
        WriteFileInput(path="pkg", content="x"), context(tmp_path)
    )

    assert output.ok is False
    assert "not a regular file" in output.content


# ------------------------------------------------------------ compacted context


async def test_the_compacted_context_tool_reads_one_window(tmp_path: Path) -> None:
    spill = MessageSpill(tmp_path)
    reference = spill.store(
        (
            Message(role=MessageRole.USER, content="first"),
            Message(role=MessageRole.SYSTEM, content="second"),
        )
    )
    output = await ReadCompactedContextTool().execute(
        ReadCompactedContextInput(reference=reference, offset=1, limit=1), context(tmp_path)
    )

    assert output.ok is True
    assert "second" in output.content
    assert "first" not in output.content


async def test_a_reference_that_was_never_stored_is_reported(tmp_path: Path) -> None:
    output = await ReadCompactedContextTool().execute(
        ReadCompactedContextInput(reference="ctx://" + "0" * 24), context(tmp_path)
    )

    assert output.ok is False
    assert "ctx://" in output.content


# ----------------------------------------------------------------------- cache


def test_an_all_system_prefix_covers_every_message() -> None:
    request = ModelRequest(
        task_id="t",
        messages=(
            Message(role=MessageRole.SYSTEM, content="a"),
            Message(role=MessageRole.SYSTEM, content="b"),
        ),
        tools=(),
    )

    plan = plan_cache(request, mode=CacheMode.PREFIX)

    assert plan.prefix_messages == 2
    assert plan.cached_fraction == 1.0


# ---------------------------------------------------------------------- loader


def test_loading_skips_children_without_a_manifest(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\nname: good\nversion: 1.0.0\ndescription: A skill\nallowed_tools: []\n"
        "smoke_command: []\n---\n\nDo it.\n"
    )
    (tmp_path / "loose.txt").write_text("not a skill")
    (tmp_path / "empty").mkdir()
    (tmp_path / "linked").symlink_to(good, target_is_directory=True)

    skills = SkillLoader().load_directory(tmp_path)

    assert [skill.manifest.name for skill in skills] == ["good"]


def test_frontmatter_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\n- a list\n---\n\nBody.\n")

    with pytest.raises(SkillLoadError, match="must be a mapping"):
        SkillLoader().load(path)


def test_a_registry_refuses_a_selection_this_deployment_cannot_run(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: one\nversion: 1.0.0\ndescription: A skill\n"
        "allowed_tools:\n  - write_file\nsmoke_command: []\n---\n\nDo it.\n"
    )
    registry = SkillRegistry((SkillLoader().load(path),))

    with pytest.raises(SkillLoadError, match="unavailable tools"):
        registry.compile(("one",), available_tools={"read_file"})


def test_a_registry_refuses_a_repeated_selection(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: one\nversion: 1.0.0\ndescription: A skill\nallowed_tools: []\n"
        "smoke_command: []\n---\n\nDo it.\n"
    )
    registry = SkillRegistry((SkillLoader().load(path),))

    with pytest.raises(SkillLoadError, match="unique"):
        registry.compile(("one", "one"), available_tools=set())


# ---------------------------------------------------------------------- adapter


def test_the_adapter_builds_its_own_client_when_none_is_given() -> None:
    model = OpenAICompatibleModel(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:9/v1", api_key="k", model="m")
    )
    try:
        assert model._client is not None
    finally:
        import asyncio

        asyncio.run(model._client.aclose())


async def test_the_adapter_refuses_a_foreign_snapshot() -> None:
    model = OpenAICompatibleModel(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:9/v1", api_key="k", model="m"),
        client=object(),  # type: ignore[arg-type]
    )
    prepared = prepare_model_call(
        model,
        ModelRequest(
            task_id="t", messages=(Message(role=MessageRole.USER, content="hi"),), tools=()
        ),
    ).model_copy(update={"format": "model-request-v1"})

    with pytest.raises(ValueError, match="unexpected prepared request format"):
        await model.decide_prepared(prepared)


async def test_a_tool_call_of_an_unknown_type_is_a_protocol_error() -> None:
    model = OpenAICompatibleModel(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:9/v1", api_key="k", model="m"),
        client=object(),  # type: ignore[arg-type]
    )
    remote = json.loads(
        '{"id": "1", "type": "code", "function": {"name": "echo", "arguments": "{}"}}'
    )
    from forgeharness.models.openai_compatible import _RemoteToolCall

    with pytest.raises(Exception, match="unsupported tool-call type"):
        model._parse_tool_call(_RemoteToolCall.model_validate(remote))


def test_a_delegated_child_inherits_the_spec_budget_without_a_parent_allowance() -> None:
    from forgeharness.runtime.subagent import SubAgentSpec, SubAgentTool

    spec = SubAgentSpec(name="helper", description="Helps", instructions="Do the task")
    tool = SubAgentTool(spec=spec, model=object(), tools=(), policy=object())  # type: ignore[arg-type]

    budget = tool._child_budget(None)

    assert budget.max_steps == spec.max_steps
    assert tool._composed_instructions() == spec.instructions


async def test_a_child_is_not_started_without_remaining_steps(tmp_path: Path) -> None:
    from forgeharness.runtime.subagent import DelegateInput, SubAgentSpec, SubAgentTool

    spec = SubAgentSpec(name="helper", description="Helps", instructions="Do the task")
    tool = SubAgentTool(spec=spec, model=object(), tools=(), policy=object())  # type: ignore[arg-type]
    starved = ToolContext(
        task_id="task",
        workspace=tmp_path,
        allowance=ExecutionAllowance(
            remaining_steps=0,
            remaining_tool_calls=0,
            remaining_input_tokens=0,
            remaining_output_tokens=0,
        ),
    )

    output = await tool.execute(DelegateInput(task="do it"), starved)

    assert output.ok is False
    assert "no remaining model steps" in output.content


def test_the_brief_precedes_an_assigned_skill() -> None:
    from forgeharness.runtime.subagent import SubAgentSpec, SubAgentTool

    spec = SubAgentSpec(
        name="helper",
        description="Helps",
        instructions="Stay inside the workspace",
        skill_instructions="A downloaded recipe",
    )
    tool = SubAgentTool(spec=spec, model=object(), tools=(), policy=object())  # type: ignore[arg-type]

    composed = tool._composed_instructions()

    assert composed.startswith("Stay inside the workspace")
    assert composed.endswith("A downloaded recipe")


def test_a_call_without_messages_has_no_outstanding_tool() -> None:
    from forgeharness.runtime.loop import _outstanding_call

    assert _outstanding_call(()) is None


def test_a_tool_needs_a_command_to_run() -> None:
    with pytest.raises(ValueError, match="test command must not be empty"):
        RunTestsTool(())


def test_a_violation_that_is_not_about_parallel_calls_is_reported_generically() -> None:
    from forgeharness.models.base import ModelProtocolError
    from forgeharness.runtime.loop import _protocol_feedback

    feedback = _protocol_feedback(ModelProtocolError("bad json", code="invalid_action"))

    assert "invalid_action" in feedback
    assert "Nothing was executed" in feedback


def test_a_recoverable_violation_explains_the_single_call_rule() -> None:
    from forgeharness.models.base import ModelProtocolError
    from forgeharness.runtime.loop import _protocol_feedback

    feedback = _protocol_feedback(
        ModelProtocolError("two calls", code="multiple_tool_calls_not_allowed")
    )

    assert "one tool call" in feedback.lower()

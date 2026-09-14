"""Tests for the LangGraph coding agent, using a scripted tool-calling model."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langchain_core")
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from forgeharness.langchain_impl.coding_agent import CodingTools, LangGraphCodingAgent


class _ScriptedToolModel(GenericFakeChatModel):
    """Fake chat model that accepts bind_tools without native tool support."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedToolModel:
        return self


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _agent(workspace: Path, messages: list[AIMessage], **kwargs: Any) -> LangGraphCodingAgent:
    model = _ScriptedToolModel(messages=iter(messages))
    return LangGraphCodingAgent(llm=model, workspace=workspace, **kwargs)


def test_coding_tools_build_bound_tools(tmp_path: Path) -> None:
    tools = CodingTools(workspace=tmp_path)
    names = {t.name for t in tools.tools}
    assert names == {"list_files", "read_file", "write_file", "run_tests", "git_diff"}


def test_read_list_run_roundtrip(tmp_path: Path) -> None:
    tools = CodingTools(workspace=tmp_path)
    (tmp_path / "calc.py").write_text("x = 1\n")

    listing = tools.tools[0].invoke({"pattern": "*.py"})
    assert "calc.py" in listing

    content = tools.tools[1].invoke({"path": "calc.py"})
    assert content == "x = 1\n"

    missing = tools.tools[1].invoke({"path": "nope.py"})
    assert "File not found" in missing


def test_write_requires_approval(tmp_path: Path) -> None:
    decisions = {"approved": False}
    calls: list[str] = []

    async def callback(action: str, path: str, content: str) -> bool:
        calls.append(path)
        return decisions["approved"]

    tools = CodingTools(workspace=tmp_path, approval_callback=callback)
    write = tools.tools[2]

    denied = asyncio.run(write.ainvoke({"path": "out.txt", "content": "hi"}))
    assert "Approval denied" in denied
    assert not (tmp_path / "out.txt").exists()

    decisions["approved"] = True
    allowed = asyncio.run(write.ainvoke({"path": "out.txt", "content": "hi"}))
    assert "Successfully wrote" in allowed
    assert (tmp_path / "out.txt").read_text() == "hi"
    assert calls == [str(tmp_path / "out.txt"), str(tmp_path / "out.txt")]


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    tools = CodingTools(workspace=tmp_path)
    with pytest.raises(ValueError, match="Path traversal"):
        tools._validate_path("../../etc/passwd")


@pytest.mark.asyncio
async def test_agent_completes_when_tests_pass(tmp_path: Path) -> None:
    messages = [
        _tool_call("read_file", {"path": "calc.py"}, "c1"),
        _tool_call("run_tests", {"command": "python -c 'print(1)'"}, "c2"),
        AIMessage(content="tests are green"),
    ]
    agent = _agent(tmp_path, messages, test_command="python -c 'print(1)'")

    result = await agent.run("verify calc.py")

    assert result["status"].value == "completed"
    assert result["tool_calls"] >= 2


@pytest.mark.asyncio
async def test_agent_fails_after_denied_write_and_failing_tests(tmp_path: Path) -> None:
    async def deny(action: str, path: str, content: str) -> bool:
        return False

    messages = [
        _tool_call("write_file", {"path": "x.txt", "content": "n"}, "c1"),
        _tool_call("run_tests", {"command": "python -c 'raise SystemExit(1)'"}, "c2"),
        AIMessage(content="trying"),
        AIMessage(content="still trying"),
    ]
    agent = _agent(
        tmp_path,
        messages,
        max_iterations=2,
        approval_callback=deny,
        test_command="python -c 'raise SystemExit(1)'",
    )

    result = await agent.run("break things")

    assert result["status"].value == "failed"
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
async def test_agent_budget_ends_run(tmp_path: Path) -> None:
    messages = [
        _tool_call("list_files", {"pattern": "*"}, "c1"),
        _tool_call("list_files", {"pattern": "*"}, "c2"),
    ]
    agent = _agent(tmp_path, messages, max_tool_calls=1, max_iterations=5)

    result = await agent.run("enumerate files")

    # The budget check ends the run before the second tool call is modeled.
    assert result["tool_calls"] <= 1
    assert result["status"].value in {"planning", "executing", "completed"}


@pytest.mark.asyncio
async def test_write_is_denied_without_approval_service(tmp_path: Path) -> None:
    tools = CodingTools(workspace=tmp_path)
    result = await tools.tools[2].ainvoke({"path": "blocked.txt", "content": "x"})
    assert "Approval required" in result
    assert not (tmp_path / "blocked.txt").exists()

"""
LangGraph-based Coding Agent with state machine.

Demonstrates:
- LangGraph StateGraph for agent workflow
- Tool integration (read, write, test, diff)
- Human-in-the-loop approval pattern
- Structured state management
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr


class AgentStatus(StrEnum):
    """Agent execution status."""

    PLANNING = "planning"
    EXECUTING = "executing"
    AWAITING_APPROVAL = "awaiting_approval"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentState(TypedDict):
    """State maintained throughout agent execution."""

    messages: list[BaseMessage]
    workspace: Path
    task: str
    test_command: str
    status: AgentStatus
    pending_approval: dict[str, Any] | None
    iterations: int
    max_iterations: int
    tool_calls: int
    max_tool_calls: int


@dataclass
class CodingTools:
    """
    Workspace-bound coding tools.

    All file operations are confined to the workspace directory. Tools are
    built as closures over the instance: a class-level ``@tool`` on a method
    would wrap the unbound function and lose ``self`` when ToolNode invokes it.
    """

    def __init__(self, workspace: Path, approval_callback: Any = None) -> None:
        self.workspace = workspace
        self.approval_callback = approval_callback

        @tool
        def list_files(pattern: str = "*") -> str:
            """List files matching pattern in workspace."""
            files = sorted(self.workspace.rglob(pattern))
            return "\n".join(str(f.relative_to(self.workspace)) for f in files[:100])

        @tool
        def read_file(path: str) -> str:
            """Read file content from workspace."""
            target = self._validate_path(path)
            if not target.exists():
                return f"Error: File not found: {path}"
            return target.read_text()

        @tool
        async def write_file(path: str, content: str) -> str:
            """
            Write file content (requires approval when a callback is configured).
            """
            target = self._validate_path(path)
            if self.approval_callback is not None:
                approved = await self.approval_callback(
                    action="write_file",
                    path=str(target),
                    content=content,
                )
                if not approved:
                    return f"Approval denied for writing {path}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return f"Successfully wrote {path}"

        @tool
        def run_tests(command: str = "") -> str:
            """Run test command in workspace."""
            cmd = command or "pytest -q"
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                output = result.stdout + result.stderr
                return f"Exit code: {result.returncode}\n{output[:2000]}"
            except subprocess.TimeoutExpired:
                return "Error: Test command timed out (60s limit)"
            except Exception as e:
                return f"Error running tests: {e}"

        @tool
        def git_diff() -> str:
            """Show git diff of changes."""
            try:
                result = subprocess.run(
                    ["git", "diff"],
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return result.stdout[:5000] or "No changes"
            except Exception as e:
                return f"Error getting diff: {e}"

        self.tools = [list_files, read_file, write_file, run_tests, git_diff]

    def _validate_path(self, relative_path: str) -> Path:
        """Ensure path is within workspace."""
        target = (self.workspace / relative_path).resolve()
        if not target.is_relative_to(self.workspace):
            raise ValueError(f"Path traversal detected: {relative_path}")
        return target


class LangGraphCodingAgent:
    """
    Coding agent implemented with LangGraph state machine.

    Workflow:
    1. Plan: Understand task and identify files to modify
    2. Execute: Read files, make changes (with approval)
    3. Test: Run test command
    4. Verify: Check diff and test results
    5. Complete or iterate
    """

    def __init__(
        self,
        llm: BaseChatModel,
        workspace: Path,
        test_command: str = "pytest -q",
        max_iterations: int = 10,
        max_tool_calls: int = 50,
        approval_callback: Any = None,
    ) -> None:
        self.llm = llm
        self.workspace = workspace
        self.test_command = test_command
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls

        # Initialize tools; writes suspend on the callback when one is wired,
        # matching the harness-wide exact-action approval philosophy.
        self.tools_instance = CodingTools(workspace, approval_callback=approval_callback)
        self.tools = list(self.tools_instance.tools)

        # Bind tools to LLM
        self.llm_with_tools = llm.bind_tools(self.tools)

        # Build graph
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        """Construct LangGraph state machine."""

        # Define graph
        workflow = StateGraph(AgentState)

        # Add nodes
        workflow.add_node("agent", self._agent_node)
        workflow.add_node("tools", ToolNode(self.tools))
        workflow.add_node("check_completion", self._check_completion)

        # Define edges
        workflow.set_entry_point("agent")

        workflow.add_conditional_edges(
            "agent",
            self._should_continue,
            {
                "continue": "tools",
                "check": "check_completion",
                "end": END,
            },
        )

        workflow.add_edge("tools", "agent")

        workflow.add_conditional_edges(
            "check_completion",
            self._completion_decision,
            {
                "complete": END,
                "iterate": "agent",
                "fail": END,
            },
        )

        return workflow.compile()

    def _agent_node(self, state: AgentState) -> dict[str, Any]:
        """Agent reasoning step."""
        # Add system prompt on first iteration
        messages = state["messages"]
        if state["iterations"] == 0:
            system_prompt = f"""You are a coding assistant. Your task:

{state["task"]}

Workspace: {state["workspace"]}
Test command: {state["test_command"]}

Process:
1. List and read relevant files
2. Make necessary changes (use write_file)
3. Run tests to verify
4. Show git diff

Work iteratively. After making changes, always run tests."""

            messages = [SystemMessage(content=system_prompt), *messages]

        # Invoke LLM with tools
        response = self.llm_with_tools.invoke(messages)

        return {
            "messages": [*messages, response],
            "iterations": state["iterations"],
            "tool_calls": state["tool_calls"] + len(getattr(response, "tool_calls", [])),
        }

    def _should_continue(self, state: AgentState) -> Literal["continue", "check", "end"]:
        """Decide next step after agent reasoning."""
        last_message = state["messages"][-1]

        # Check budget limits
        if state["iterations"] >= state["max_iterations"]:
            return "end"
        if state["tool_calls"] >= state["max_tool_calls"]:
            return "end"

        # If agent called tools, execute them
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "continue"

        # If agent finished reasoning, check completion
        return "check"

    def _check_completion(self, state: AgentState) -> dict[str, Any]:
        """Check if task is complete by examining test results."""
        messages = state["messages"]

        # Look for recent test results: ToolNode reports the tool name on the
        # ToolMessage `name` field, not in the content.
        recent_messages = messages[-10:]
        test_output = None
        for msg in reversed(recent_messages):
            if getattr(msg, "name", "") == "run_tests":
                test_output = str(msg.content)
                break

        # Simple heuristic: tests passed if exit code 0
        tests_passed = test_output is not None and "Exit code: 0" in test_output

        if tests_passed:
            status = AgentStatus.COMPLETED
        elif state["iterations"] >= state["max_iterations"] - 1:
            status = AgentStatus.FAILED
        else:
            status = AgentStatus.EXECUTING

        return {
            "status": status,
            "iterations": state["iterations"] + 1,
        }

    def _completion_decision(self, state: AgentState) -> Literal["complete", "iterate", "fail"]:
        """Decide whether to complete, iterate, or fail."""
        if state["status"] == AgentStatus.COMPLETED:
            return "complete"
        elif state["status"] == AgentStatus.FAILED:
            return "fail"
        else:
            return "iterate"

    async def run(self, task: str) -> dict[str, Any]:
        """
        Execute coding task.

        Returns final state with status and complete message history.
        """
        initial_state: AgentState = {
            "messages": [HumanMessage(content=task)],
            "workspace": self.workspace,
            "task": task,
            "test_command": self.test_command,
            "status": AgentStatus.PLANNING,
            "pending_approval": None,
            "iterations": 0,
            "max_iterations": self.max_iterations,
            "tool_calls": 0,
            "max_tool_calls": self.max_tool_calls,
        }

        # Run graph
        final_state = await self.graph.ainvoke(initial_state)

        return {
            "status": final_state["status"],
            "iterations": final_state["iterations"],
            "tool_calls": final_state["tool_calls"],
            "messages": final_state["messages"],
        }


# Demo
async def demo() -> None:  # pragma: no cover - manual smoke script
    """Demonstrate LangGraph coding agent."""
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        base_url="http://127.0.0.1:8000/v1",
        model="Qwen3.5-9B-4bit",
        temperature=0.1,
        api_key=SecretStr("not-needed"),
    )

    # Use a test workspace
    workspace = Path("/tmp/langgraph-agent-demo")
    await asyncio.to_thread(workspace.mkdir, exist_ok=True)

    # Create a simple test file
    demo_file = workspace / "test_demo.py"
    await asyncio.to_thread(
        demo_file.write_text,
        """
def add(a, b):
    return a + b

def test_add():
    assert add(2, 2) == 4
""",
    )

    agent = LangGraphCodingAgent(
        llm=llm,
        workspace=workspace,
        test_command="python -m pytest test_demo.py -v",
    )

    print("=== LangGraph Coding Agent Demo ===\n")

    result = await agent.run("Add a subtract function and write a test for it in test_demo.py")

    print(f"Status: {result['status']}")
    print(f"Iterations: {result['iterations']}")
    print(f"Tool calls: {result['tool_calls']}")
    print("\nFinal diff:")

    # Show the changes
    import subprocess

    diff = await asyncio.to_thread(
        subprocess.run,
        ["git", "diff", "test_demo.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    print(diff.stdout or "No git repo")


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    asyncio.run(demo())

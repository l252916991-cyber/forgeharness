"""CLI contract tests: every command documented in README must exist and parse."""

from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command
from typer.testing import CliRunner

from forgeharness.cli import app

README = Path(__file__).resolve().parents[2] / "README.md"

DOCUMENTED_COMMANDS = (
    "review",
    "demo",
    "eval-control",
    "eval-rag",
    "eval-reviewer",
    "bench-retrieval",
    "qualify-omlx",
    "repair",
    "inspect-run",
    "serve",
    "langchain-rag",
    "langchain-agent",
    "framework-compare",
)


def _registered_command_names() -> set[str]:
    """Resolve names through Typer's own build path (authoritative)."""
    click_app = get_command(app)
    return set(getattr(click_app, "commands", {}).keys())


def _readme_declares(command: str) -> bool:
    """True when README.md contains a `forge <command>` invocation."""
    return re.search(rf"forge\s+{re.escape(command)}\b", README.read_text()) is not None


def test_every_documented_command_is_registered() -> None:
    """README promises must never outrun the CLI surface (regression for P0-1)."""
    registered = _registered_command_names()
    missing = [
        cmd for cmd in DOCUMENTED_COMMANDS if _readme_declares(cmd) and cmd not in registered
    ]
    assert not missing, f"README documents commands missing from CLI: {missing}"


def test_readme_does_not_document_unregistered_commands() -> None:
    """Inverse check: scan README for `forge <word>` and verify each is registered."""
    referenced = set(re.findall(r"\bforge\s+([a-z][a-z0-9-]+)", README.read_text()))
    unregistered = referenced - set(DOCUMENTED_COMMANDS) - _registered_command_names()
    assert not unregistered, f"README references unregistered commands: {sorted(unregistered)}"


def test_framework_commands_declared_options_parse() -> None:
    """Framework commands expose the documented options even without the extra."""
    runner = CliRunner()
    for command in ("langchain-rag", "langchain-agent", "framework-compare"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, f"{command} --help failed: {result.output}"

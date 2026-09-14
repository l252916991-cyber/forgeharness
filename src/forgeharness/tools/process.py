"""Cancellation-safe subprocess execution with a sanitized environment."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from forgeharness.domain.models import FrozenModel


class CommandResult(FrozenModel):
    """Bounded subprocess evidence returned by a fixed-purpose tool."""

    exit_code: int
    output: str
    truncated: bool


def sanitized_environment() -> dict[str, str]:
    """Keep execution essentials while excluding credentials by default."""
    allowed = ("PATH", "VIRTUAL_ENV", "PYTHONPATH", "LANG", "LC_ALL", "TMPDIR")
    return {name: os.environ[name] for name in allowed if name in os.environ}


async def run_command(
    command: tuple[str, ...], *, workspace: Path, max_output_bytes: int = 50_000
) -> CommandResult:
    """Run a preconstructed argv vector and terminate the child when cancelled."""
    if not command:
        raise ValueError("command must not be empty")
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env=sanitized_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    captured = bytearray()
    truncated = False
    try:
        assert process.stdout is not None
        while chunk := await process.stdout.read(64 * 1024):
            remaining = max_output_bytes - len(captured)
            captured.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
        await process.wait()
    except asyncio.CancelledError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    if process.returncode is None:
        raise RuntimeError("subprocess completed without an exit code")
    return CommandResult(
        exit_code=process.returncode,
        output=captured.decode("utf-8", errors="replace"),
        truncated=truncated,
    )

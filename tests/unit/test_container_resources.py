"""Resource limits apply while processes run and while pool callers race."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from forgeharness.backends import container
from forgeharness.backends.container import ContainerLimits, WarmContainerPool
from forgeharness.backends.protocol import BackendError
from forgeharness.tools.process import CommandResult


def ok(output=""):
    return CommandResult(exit_code=0, output=output, truncated=False)


async def test_spawn_streams_large_output_without_communicate(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("communicate buffers unbounded subprocess output")

    monkeypatch.setattr(asyncio.subprocess.Process, "communicate", forbidden)
    result = await container._spawn(
        (sys.executable, "-c", "import os; [os.write(1, b'x' * 65536) for _ in range(160)]"),
        timeout_seconds=5,
        max_output_bytes=4096,
    )
    assert result.exit_code == 0
    assert result.output == "x" * 4096
    assert result.truncated


async def test_spawn_timeout_kills_and_reaps_the_child():
    result = await container._spawn(
        (sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(30)"),
        timeout_seconds=0.5,
    )
    assert result.exit_code == 124
    assert result.output.endswith("[container command timed out]")
    pid = int(result.output.splitlines()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_spawn_cancellation_kills_and_reaps_the_child(tmp_path):
    marker = tmp_path / "child.pid"
    task = asyncio.create_task(
        container._spawn(
            (
                sys.executable,
                "-c",
                "import os,sys,time; "
                "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)",
                str(marker),
            ),
            timeout_seconds=10,
        )
    )
    try:
        async with asyncio.timeout(5):
            # The readiness signal belongs to an independent OS process.
            while not marker.exists():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize(
    "argv,target",
    [
        (("docker", "run", "--rm", "--name", "one-shot", "image", "sleep"), "one-shot"),
        (("docker", "run", "--detach", "--rm", "--name", "starting", "image"), "starting"),
        (("docker", "exec", "--workdir", "/workspace", "pooled", "sleep"), "pooled"),
    ],
)
async def test_docker_runner_removes_daemon_container_after_timeout_or_cancel(
    monkeypatch, argv, target, cancelled
):
    seen = []

    async def spawn(command, *, timeout_seconds, max_output_bytes=50_000):
        seen.append(command)
        if command == argv:
            if cancelled:
                raise asyncio.CancelledError
            return CommandResult(exit_code=124, output="timed out", truncated=False)
        assert timeout_seconds <= 5
        return ok()

    monkeypatch.setattr(container, "_spawn", spawn)
    runner = container.docker_runner()
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await runner(argv)
    else:
        assert (await runner(argv)).exit_code == 124
    assert seen == [argv, ("docker", "rm", "--force", target)]


async def test_command_arguments_cannot_select_a_cleanup_target():
    assert container._cleanup_target(("docker", "run", "--rm", "image", "--name", "other")) is None


async def test_pool_capacity_applies_to_busy_containers_and_warm(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    starts = 0

    async def runner(argv):
        nonlocal starts
        if argv[1] == "run":
            starts += 1
            return ok(f"container-{starts}")
        if argv[1] == "exec":
            entered.set()
            await release.wait()
        return ok()

    pool = WarmContainerPool(
        limits=ContainerLimits(), image="image", root=tmp_path, size=1, runner=runner
    )
    first = asyncio.create_task(pool.run(("sleep",)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await pool.warm()
        for _ in range(3):
            with pytest.raises(BackendError, match="at capacity"):
                await pool.run(("other",))
        assert starts == 1
        assert pool.live_containers == ("container-1",)
        release.set()
        await first
    finally:
        release.set()
        await pool.aclose()
        await asyncio.gather(first, return_exceptions=True)


async def test_pool_close_cancels_busy_commands_and_refuses_reuse(tmp_path):
    entered = asyncio.Event()
    removed = []

    async def runner(argv):
        if argv[1] == "run":
            return ok("busy-container")
        if argv[1] == "exec":
            entered.set()
            await asyncio.Event().wait()
        if argv[1] == "rm":
            removed.append(argv[-1])
        return ok()

    pool = WarmContainerPool(limits=ContainerLimits(), image="image", root=tmp_path, runner=runner)
    busy = asyncio.create_task(pool.run(("sleep",)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.wait_for(pool.aclose(), timeout=1)
    assert busy.cancelled()
    assert removed == ["busy-container"]
    assert pool.live_containers == ()
    for operation in (pool.warm, lambda: pool.run(("other",))):
        with pytest.raises(BackendError, match="closed"):
            await operation()
    await pool.aclose()


async def test_pool_close_cancels_startup_without_waiting_for_its_io_lock(tmp_path):
    entered = asyncio.Event()
    starts, removed = [], []

    async def runner(argv):
        if argv[1] == "run":
            starts.append(argv[argv.index("--name") + 1])
            entered.set()
            await asyncio.Event().wait()
        if argv[1] == "rm":
            removed.append(argv[-1])
        return ok()

    pool = WarmContainerPool(limits=ContainerLimits(), image="image", root=tmp_path, runner=runner)
    starting = asyncio.create_task(pool.warm())
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(BackendError, match="at capacity"):
        await pool.run(("other",))
    await asyncio.wait_for(pool.aclose(), timeout=1)
    assert starting.cancelled()
    assert removed == starts
    assert pool.live_containers == ()


async def test_pool_close_finishes_daemon_cleanup_when_the_closer_is_cancelled(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    removed = []

    async def runner(argv):
        if argv[1] == "run":
            return ok("idle-container")
        if argv[1] == "rm":
            entered.set()
            await release.wait()
            removed.append(argv[-1])
        return ok()

    pool = WarmContainerPool(limits=ContainerLimits(), image="image", root=tmp_path, runner=runner)
    await pool.warm()
    closing = asyncio.create_task(pool.aclose())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=1)
        assert removed == ["idle-container"]
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_container_backend_bounds_utf8_bytes_and_names_one_shot(tmp_path: Path):
    seen = []

    async def runner(argv):
        seen.append(argv)
        return ok("界" * 100)

    backend = container.ContainerBackend(tmp_path, image="image", runner=runner)
    result = await backend.run(("echo", "text"), max_output_bytes=4)
    assert result.output == "界\n[tool output truncated]"
    assert result.truncated
    assert seen[0][:4] == ("docker", "run", "--rm", "--name")


async def test_failed_container_removal_keeps_its_capacity_slot_until_cleanup_succeeds(tmp_path):
    cleanup_succeeds = False
    starts = 0

    async def runner(argv):
        nonlocal starts
        if argv[1] == "run":
            starts += 1
            return ok("uncertain-container")
        if argv[1] == "exec":
            return CommandResult(exit_code=124, output="timeout", truncated=False)
        return ok() if cleanup_succeeds else CommandResult(exit_code=1, output="", truncated=False)

    pool = WarmContainerPool(limits=ContainerLimits(), image="image", root=tmp_path, runner=runner)
    with pytest.raises(BackendError, match="cleanup did not complete"):
        await pool.run(("sleep",))
    assert pool.live_containers == ("uncertain-container",)
    with pytest.raises(BackendError, match="at capacity"):
        await pool.run(("other",))
    with pytest.raises(BackendError, match="cleanup did not complete"):
        await pool.aclose()
    assert starts == 1
    assert pool.live_containers == ("uncertain-container",)
    cleanup_succeeds = True
    await pool.aclose()
    assert pool.live_containers == ()

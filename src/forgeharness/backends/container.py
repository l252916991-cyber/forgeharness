"""An execution backend that confines *running code* to a container.

``tools/policy.py`` can refuse a path and refuse a command shape, and it says in its
own comment that this is defence in depth rather than a sandbox. It is right: a policy
cannot stop a permitted command from deleting files it is allowed to reach, and it
cannot stop that command from running on the operator's machine. The only honest answer
is to make "permitted" mean "inside a container", and to make the container's limits
part of the request rather than part of the operator's diligence.

What the argv below buys, and why each flag is not optional:

``--network none``
    No egress. A test command cannot exfiltrate a workspace or fetch a payload.
``--read-only`` plus one read-write bind mount
    The container's own filesystem is immutable; only the workspace is writable, so a
    command can dirty the repository -- which is the point -- and nothing else.
``--user 1000:1000``
    Not root. Combined with ``--cap-drop ALL`` and ``no-new-privileges`` there is no
    capability to escalate with and no setuid path to escalate through.
``--memory``/``--memory-swap``/``--cpus``/``--pids-limit``
    A runaway test suite is the common accident, not the exotic one. Equal memory and
    swap bound the container without letting the kernel swap instead of OOM-killing.
``--init``
    A real PID 1 that reaps orphans, so a command that forks does not leak processes
    past the container's life.

The command is always an argv vector. Nothing here ever builds a shell line, so a model
cannot smuggle ``; rm -rf /`` through an argument: the argument is a word, not code.

File reads and writes still go through :class:`LocalBackend` over the bind mount. That
is deliberate: the harness's own file tools never execute repository code, so the
threat they face is a wrong path -- already refused -- while the threat that needs a
container is executing repository code. Saying this out loud matters, because a reader
who assumes writes are also containerised would trust the wrong boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from forgeharness.backends.local import LocalBackend
from forgeharness.backends.protocol import (
    BackendError,
    FileEntry,
    ReadResult,
    WriteResult,
)
from forgeharness.domain.models import FrozenModel
from forgeharness.tools.process import CommandResult, sanitized_environment

#: Where the workspace is mounted inside the container. Fixed so a command's relative
#: paths mean the same thing on both sides of the boundary.
CONTAINER_WORKDIR = "/workspace"

#: Environment variable names forwarded into the container. ``PATH`` is deliberately
#: absent: the image's own PATH is correct, and forwarding the host's would point at
#: binaries that do not exist inside.
FORWARDED_ENVIRONMENT = ("LANG", "LC_ALL", "TZ")

#: A command runner takes a full argv and returns bounded output. Injecting it is what
#: lets the container policy be tested without a Docker daemon.
Runner = Callable[[tuple[str, ...]], Awaitable[CommandResult]]


class ContainerLimits(FrozenModel):
    """The resource and isolation envelope of one sandboxed command."""

    #: Only ``none`` is a sandbox. ``bridge`` exists for the rare case that a build
    #: genuinely needs the network, and choosing it is a decision the operator makes
    #: explicitly rather than a default that quietly leaks.
    network: Literal["none", "bridge"] = "none"
    #: Immutable root filesystem; the workspace bind mount is the only writable path.
    read_only_rootfs: bool = True
    memory: str = Field(default="512m", pattern=r"^[0-9]+[bkmgBKMG]?$")
    #: Kept equal to ``memory`` so the kernel cannot swap instead of enforcing the cap.
    memory_swap: str | None = None
    cpus: float = Field(default=1.0, gt=0, le=64)
    pids_limit: int = Field(default=256, ge=16, le=32_768)
    user: str = Field(default="1000:1000", pattern=r"^[0-9]+(:[0-9]+)?$")
    cap_drop: tuple[str, ...] = ("ALL",)
    no_new_privileges: bool = True
    #: ``noexec,nosuid`` on /tmp stops a command from staging a binary it then runs.
    tmpfs: str = Field(default="/tmp:rw,noexec,nosuid,size=64m")
    timeout_seconds: float = Field(default=300.0, gt=0)

    @model_validator(mode="after")
    def _swap_matches_memory(self) -> ContainerLimits:
        # An unset --memory-swap lets the container use host swap, which turns the
        # memory cap into a slowdown instead of a limit.
        if self.memory_swap is None:
            object.__setattr__(self, "memory_swap", self.memory)
        return self

    @model_validator(mode="after")
    def _never_root(self) -> ContainerLimits:
        if self.user.split(":", 1)[0] == "0":
            raise ValueError("a sandboxed command must not run as root")
        return self


def _limits_flags(limits: ContainerLimits) -> list[str]:
    """The isolation flags shared by one-shot runs and pooled containers."""
    flags = [
        "--init",
        "--network",
        limits.network,
        "--memory",
        limits.memory,
        "--memory-swap",
        limits.memory_swap or limits.memory,
        "--cpus",
        str(limits.cpus),
        "--pids-limit",
        str(limits.pids_limit),
        "--user",
        limits.user,
        "--tmpfs",
        limits.tmpfs,
    ]
    for capability in limits.cap_drop:
        flags += ["--cap-drop", capability]
    if limits.no_new_privileges:
        flags += ["--security-opt", "no-new-privileges"]
    if limits.read_only_rootfs:
        flags.append("--read-only")
    for name in FORWARDED_ENVIRONMENT:
        value = os.environ.get(name)
        if value is not None:
            flags += ["--env", f"{name}={value}"]
    return flags


def container_argv(
    limits: ContainerLimits,
    *,
    image: str,
    command: tuple[str, ...],
    root: Path,
    name: str | None = None,
) -> tuple[str, ...]:
    """Build the ``docker run`` argv for one command in the workspace.

    The workspace mount is asserted to be absolute and canonical: a relative ``-v``
    would be resolved against the daemon's cwd, which is a different directory than
    the caller means, and a path containing ``..`` would mount an ancestor.
    """
    if not image:
        raise ValueError("image must not be empty")
    if not command:
        raise ValueError("command must not be empty")
    resolved = Path(root).resolve(strict=True)
    argv = ["docker", "run", "--rm"]
    if name is not None:
        argv += ["--name", name]
    argv += _limits_flags(limits)
    argv += [
        "--volume",
        f"{resolved}:{CONTAINER_WORKDIR}:rw",
        "--workdir",
        CONTAINER_WORKDIR,
        image,
        *command,
    ]
    return tuple(argv)


def pooled_argv(
    limits: ContainerLimits, *, image: str, root: Path, name: str, command: tuple[str, ...]
) -> tuple[str, ...]:
    """Build the ``docker run -d`` argv that starts a reusable pool container."""
    argv = ["docker", "run", "--detach", "--rm", "--name", name]
    argv += _limits_flags(limits)
    argv += [
        "--volume",
        f"{Path(root).resolve(strict=True)}:{CONTAINER_WORKDIR}:rw",
        "--workdir",
        CONTAINER_WORKDIR,
        image,
        *command,
    ]
    return tuple(argv)


def exec_argv(container_id: str, command: tuple[str, ...]) -> tuple[str, ...]:
    """Build the ``docker exec`` argv that runs a command in a pooled container."""
    if not container_id:
        raise ValueError("container id must not be empty")
    if not command:
        raise ValueError("command must not be empty")
    return ("docker", "exec", "--workdir", CONTAINER_WORKDIR, container_id, *command)


async def _spawn(
    argv: tuple[str, ...], *, timeout_seconds: float, max_output_bytes: int = 50_000
) -> CommandResult:
    """Drain continuously while retaining only a bounded prefix of child output."""
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=sanitized_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    captured = bytearray()
    truncated = False

    async def drain() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while chunk := await process.stdout.read(64 * 1024):
            remaining = max_output_bytes - len(captured)
            captured.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining

    # Shield both tasks from the command deadline so the reader keeps draining
    # after SIGKILL. Waiting for a full pipe's process before draining can hang.
    draining = asyncio.create_task(drain())
    waiting = asyncio.create_task(process.wait())
    completion = asyncio.gather(draining, waiting)

    async def stop() -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.shield(completion), timeout=5)
        except TimeoutError:
            # A detached descendant may retain the pipe after the CLI is dead.
            # asyncio exposes no public pipe-close API on Process; close its
            # transport rather than retaining that inherited descriptor forever.
            process._transport.close()  # type: ignore[attr-defined]
            completion.cancel()
            await asyncio.gather(draining, waiting, return_exceptions=True)

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(completion), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        await stop()
    except BaseException:
        await stop()
        raise
    output = captured.decode("utf-8", errors="replace")
    if timed_out:
        output += "\n[container command timed out]"
    return CommandResult(
        exit_code=124 if timed_out else (process.returncode or 0),
        output=output,
        truncated=truncated,
    )


def _cleanup_target(argv: tuple[str, ...]) -> str | None:
    """Only recognize argv prefixes constructed by this module, never tool flags."""
    if argv[:4] == ("docker", "run", "--rm", "--name") and len(argv) > 4:
        return argv[4]
    if argv[:5] == ("docker", "run", "--detach", "--rm", "--name") and len(argv) > 5:
        return argv[5]
    if argv[:4] == ("docker", "exec", "--workdir", CONTAINER_WORKDIR) and len(argv) > 4:
        return argv[4]
    return None


def docker_runner(timeout_seconds: float = 300.0, *, cleanup_containers: bool = True) -> Runner:
    """The production runner: a real ``docker`` subprocess."""

    async def run(argv: tuple[str, ...]) -> CommandResult:
        target = _cleanup_target(argv) if cleanup_containers else None

        async def remove() -> None:
            if target is not None:
                removed = await _spawn(("docker", "rm", "--force", target), timeout_seconds=5)
                if removed.exit_code != 0:
                    raise BackendError("sandbox container cleanup did not complete")

        try:
            result = await _spawn(argv, timeout_seconds=timeout_seconds)
        except BaseException:
            await remove()
            raise
        if result.exit_code == 124:
            await remove()
        return result

    return run


class WarmContainerPool:
    """A small pool of already-started containers, one command at a time each.

    Starting a container costs a second or more; a test-heavy run starts hundreds. The
    pool starts containers ahead of demand and returns them after each command, which
    keeps the isolation identical -- every command still runs inside a container with
    the same limits -- while removing the startup cost from the critical path.

    A container that produced a timeout is discarded rather than reused: a command that
    ignored a kill signal has an unknown process tree, and handing that to the next
    caller would leak state between tasks.
    """

    def __init__(
        self,
        *,
        limits: ContainerLimits,
        image: str,
        root: Path,
        size: int = 1,
        runner: Runner | None = None,
        keepalive: tuple[str, ...] = ("sleep", "infinity"),
    ) -> None:
        if type(size) is not int or size < 1:
            raise ValueError("pool size must be at least 1")
        self._limits = limits
        self._image = image
        self._root = Path(root)
        self._size = size
        # The pool owns retirement and capacity accounting, including failed
        # cleanup. Its runner must not independently remove the same container.
        self._runner = runner or docker_runner(limits.timeout_seconds, cleanup_containers=False)
        self._keepalive = keepalive
        self._idle: list[str] = []
        self._live: set[str] = set()
        self._busy: dict[str, asyncio.Task[object]] = {}
        self._starting: dict[asyncio.Task[object], str] = {}
        self._retiring: set[str] = set()
        self._retirement_tasks: dict[str, asyncio.Task[object]] = {}
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def live_containers(self) -> tuple[str, ...]:
        """Identifiers currently owned by the pool, idle or busy."""
        return tuple(sorted(self._live))

    async def _start_one(self, name: str) -> str:
        try:
            result = await self._runner(
                pooled_argv(
                    self._limits,
                    image=self._image,
                    root=self._root,
                    name=name,
                    command=self._keepalive,
                )
            )
        except BaseException:
            await self._remove_unadopted(name)
            raise
        if result.exit_code != 0:
            try:
                await self._remove_unadopted(name)
            except Exception as exc:
                raise BackendError("could not start sandbox container; cleanup pending") from exc
            raise BackendError(f"could not start sandbox container: {result.output.strip()}")
        container_id = result.output.strip().splitlines()[-1] if result.output.strip() else ""
        if not container_id or result.truncated:
            await self._remove_unadopted(name)
            raise BackendError("docker did not report a container id")
        return container_id

    def _reserve_start(self, task: asyncio.Task[object]) -> str:
        if len(self._live) + len(self._starting) >= self._size:
            raise BackendError("sandbox pool is at capacity")
        name = f"forgeharness-{uuid.uuid4().hex[:12]}"
        self._starting[task] = name
        return name

    async def _create(self, name: str, task: asyncio.Task[object], *, busy: bool) -> str:
        container_id = None
        try:
            container_id = await self._start_one(name)
            async with self._lock:
                if self._closed:
                    raise BackendError("sandbox pool is closed")
                self._live.add(container_id)
                if busy:
                    self._busy[container_id] = task
                else:
                    self._idle.append(container_id)
            return container_id
        except BaseException:
            if container_id is not None:
                await self._remove_unadopted(container_id)
            raise
        finally:
            async with self._lock:
                self._starting.pop(task, None)

    async def warm(self) -> None:
        """Start containers up to the configured size, so the first command is fast."""
        task = asyncio.current_task()
        assert task is not None
        while True:
            async with self._lock:
                if self._closed:
                    raise BackendError("sandbox pool is closed")
                if len(self._live) + len(self._starting) >= self._size:
                    return
                name = self._reserve_start(task)
            await self._create(name, task, busy=False)

    async def run(self, command: tuple[str, ...]) -> CommandResult:
        """Run within the fixed capacity; refuse excess demand without queuing."""
        if not command:
            raise ValueError("command must not be empty")
        task = asyncio.current_task()
        assert task is not None
        name = None
        async with self._lock:
            if self._closed:
                raise BackendError("sandbox pool is closed")
            if self._idle:
                container_id = self._idle.pop()
                self._busy[container_id] = task
            else:
                name = self._reserve_start(task)
        if name is not None:
            container_id = await self._create(name, task, busy=True)
        try:
            result = await self._runner(exec_argv(container_id, command))
        except BaseException:
            await self._discard(container_id)
            raise
        if result.exit_code == 124:
            await self._discard(container_id)
            return result
        async with self._lock:
            self._busy.pop(container_id, None)
            if container_id in self._live:
                self._idle.append(container_id)
        return result

    async def _remove(self, container_id: str) -> None:
        # Cleanup itself has a bound even for an injected or unavailable runner.
        async with asyncio.timeout(10):
            result = await self._runner(("docker", "rm", "--force", container_id))
        if result.exit_code != 0:
            raise BackendError("sandbox container cleanup did not complete")

    async def _remove_unadopted(self, container_id: str) -> None:
        try:
            await self._remove(container_id)
        except BaseException:
            # An uncertain startup still consumes a slot. Forgetting a failed
            # removal would permit unlimited daemon containers despite size=1.
            async with self._lock:
                self._live.add(container_id)
            raise

    async def _remove_owned(self, container_id: str) -> None:
        try:
            await self._remove(container_id)
        except BaseException:
            async with self._lock:
                self._retiring.discard(container_id)
                self._retirement_tasks.pop(container_id, None)
            raise
        async with self._lock:
            self._live.discard(container_id)
            self._retiring.discard(container_id)
            self._retirement_tasks.pop(container_id, None)

    async def _discard(self, container_id: str) -> None:
        async with self._lock:
            owned = container_id in self._live and container_id not in self._retiring
            if owned:
                self._retiring.add(container_id)
                task = asyncio.current_task()
                assert task is not None
                self._retirement_tasks[container_id] = task
            self._busy.pop(container_id, None)
            self._idle = [live for live in self._idle if live != container_id]
        if owned:
            await self._remove_owned(container_id)

    async def aclose(self) -> None:
        """Remove every container the pool owns. Safe to call twice."""
        async with self._lock:
            self._closed = True
            owned = list(self._live - self._retiring)
            self._retiring.update(owned)
            busy = tuple(set(self._busy.values()) | set(self._starting))
            retiring = tuple(set(self._retirement_tasks.values()))
            self._idle = []
            self._busy.clear()
        current = asyncio.current_task()
        for task in busy:
            if task is not current:
                task.cancel()

        async def finish() -> None:
            outcomes = await asyncio.gather(
                *(self._remove_owned(container_id) for container_id in owned),
                *(task for task in busy if task is not current),
                *(task for task in retiring if task is not current),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError
                ):
                    raise BackendError("sandbox container cleanup did not complete") from outcome

        cleanup = asyncio.create_task(finish())
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                # Cleanup has its own deadlines. Cancellation of the owner must
                # not abandon already-created containers in the daemon.
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


class ContainerBackend:
    """Execute commands in a container; serve files over the same bind mount."""

    name = "container"

    def __init__(
        self,
        root: Path,
        *,
        limits: ContainerLimits | None = None,
        image: str,
        runner: Runner | None = None,
        pool: WarmContainerPool | None = None,
    ) -> None:
        self._files = LocalBackend(root)
        self._limits = limits or ContainerLimits()
        self._image = image
        self._root = Path(root)
        # A pool is owned by whoever constructed it: a backend must never start
        # containers it has no way to close.
        self._pool = pool
        self._runner = runner or docker_runner(self._limits.timeout_seconds)

    @property
    def files(self) -> LocalBackend:
        """The bind-mounted view used for reads and writes."""
        return self._files

    def list_files(
        self, *, path: str = ".", limit: int = 200, skip_directories: frozenset[str] = frozenset()
    ) -> tuple[FileEntry, ...]:
        return self._files.list_files(path=path, limit=limit, skip_directories=skip_directories)

    def read_text(self, path: str, *, max_bytes: int = 1_000_000) -> ReadResult:
        return self._files.read_text(path, max_bytes=max_bytes)

    def write_text(
        self, path: str, text: str, *, expected_digest: str | None = None
    ) -> WriteResult:
        return self._files.write_text(path, text, expected_digest=expected_digest)

    def digest_of(self, path: str) -> str:
        return self._files.digest_of(path)

    async def search(
        self, query: str, *, path: str = ".", max_output_bytes: int = 30_000
    ) -> CommandResult:
        """Search the bind mount through the same scoped file boundary as reads."""
        return await self._files.search(query, path=path, max_output_bytes=max_output_bytes)

    async def run(
        self, command: tuple[str, ...], *, max_output_bytes: int = 50_000
    ) -> CommandResult:
        """Run ``command`` under the container limits, with bounded output."""
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        if self._pool is not None:
            result = await self._pool.run(command)
        else:
            result = await self._runner(
                container_argv(
                    self._limits,
                    image=self._image,
                    command=command,
                    root=self._root,
                    name=f"forgeharness-{uuid.uuid4().hex[:12]}",
                )
            )
        encoded = result.output.encode("utf-8")
        if len(encoded) > max_output_bytes:
            return CommandResult(
                exit_code=result.exit_code,
                output=encoded[:max_output_bytes].decode("utf-8", errors="ignore")
                + "\n[tool output truncated]",
                truncated=True,
            )
        return result


def container_backend_factory(
    *, image: str, limits: ContainerLimits | None = None, runner: Runner | None = None
) -> Callable[[Path], ContainerBackend]:
    """Build a factory so each call's workspace gets its own sandboxed backend."""
    frozen = limits or ContainerLimits()

    def factory(root: Path) -> ContainerBackend:
        return ContainerBackend(root, limits=frozen, image=image, runner=runner)

    return factory


__all__ = [
    "CONTAINER_WORKDIR",
    "ContainerBackend",
    "ContainerLimits",
    "Runner",
    "WarmContainerPool",
    "container_argv",
    "container_backend_factory",
    "docker_runner",
    "exec_argv",
    "pooled_argv",
]

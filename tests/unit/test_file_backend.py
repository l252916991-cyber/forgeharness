"""Tests for the file backend protocol, its implementations, and container confinement."""

import asyncio
from pathlib import Path

import pytest

from forgeharness.backends.composite import CompositeBackend, composite_factory
from forgeharness.backends.container import (
    CONTAINER_WORKDIR,
    ContainerBackend,
    ContainerLimits,
    WarmContainerPool,
    container_argv,
    container_backend_factory,
    exec_argv,
    pooled_argv,
)
from forgeharness.backends.local import DEFAULT_SKIP_DIRECTORIES, LocalBackend
from forgeharness.backends.protocol import BackendError, FileConflict, FileTooLarge, NotRegularFile
from forgeharness.tools.process import CommandResult


def seed(root: Path) -> Path:
    (root / "src").mkdir()
    (root / "src" / "service.py").write_text("def calculate():\n    return 41\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret")
    (root / ".forgeharness").mkdir()
    (root / ".forgeharness" / "state.json").write_text("{}")
    return root


# --- local backend -----------------------------------------------------------


def test_local_backend_lists_and_reads_within_the_workspace(tmp_path: Path) -> None:
    backend = LocalBackend(seed(tmp_path))

    entries = backend.list_files()
    paths = [entry.path for entry in entries]

    assert "src/service.py" in paths
    # Harness state and VCS internals are not part of the task workspace.
    assert not [
        path for path in paths if path.startswith(".git") or path.startswith(".forgeharness")
    ]
    read = backend.read_text("src/service.py")
    assert read.text.startswith("def calculate")
    assert read.size == len(read.text.encode())
    assert backend.digest_of("src/service.py") == read.digest
    assert DEFAULT_SKIP_DIRECTORIES == frozenset({".git", ".forgeharness"})


def test_local_backend_refuses_to_escape_the_workspace(tmp_path: Path) -> None:
    backend = LocalBackend(seed(tmp_path))

    for attempt in ("../outside.txt", "/etc/passwd", "src/../../etc/passwd"):
        with pytest.raises(ValueError, match=r"escapes the task workspace|absolute"):
            backend.read_text(attempt)


def test_local_backend_writes_are_optimistic_and_atomic(tmp_path: Path) -> None:
    backend = LocalBackend(seed(tmp_path))
    existing = backend.read_text("src/service.py")

    with pytest.raises(FileConflict, match="requires expected_sha256"):
        backend.write_text("src/service.py", "changed")
    with pytest.raises(FileConflict, match="changed since it was read"):
        backend.write_text("src/service.py", "changed", expected_digest="0" * 64)

    written = backend.write_text("src/service.py", "changed", expected_digest=existing.digest)
    assert written.created is False

    created = backend.write_text("src/new.py", "print('hi')\n")
    assert created.created is True
    # A fresh file must not claim to be an update of something.
    with pytest.raises(FileConflict, match="new file must not set expected_sha256"):
        backend.write_text("src/other.py", "x", expected_digest="1" * 64)


def test_local_backend_rejects_directory_reads_and_oversized_files(tmp_path: Path) -> None:
    backend = LocalBackend(seed(tmp_path))
    (tmp_path / "big.txt").write_text("x" * 64)

    with pytest.raises(NotRegularFile):
        backend.read_text("src")
    with pytest.raises(FileTooLarge):
        backend.read_text("big.txt", max_bytes=16)


def test_local_backend_refuses_to_hash_a_symlink(tmp_path: Path) -> None:
    backend = LocalBackend(seed(tmp_path))
    (tmp_path / "link").symlink_to(tmp_path / "src" / "service.py")

    with pytest.raises(NotRegularFile):
        backend.digest_of("link")


async def test_local_backend_runs_a_command_and_reports_no_matches(tmp_path: Path) -> None:
    backend = LocalBackend(seed(tmp_path))

    found = await backend.search("calculate")
    missing = await backend.search("definitely-not-present")

    assert found.exit_code == 0
    assert "src/service.py" in found.output
    assert missing.exit_code == 1
    assert missing.output.strip() == ""


# --- container argv: the six escapes the flags exist to stop ------------------


def test_container_argv_denies_egress_root_write_capabilities_and_swap(tmp_path: Path) -> None:
    resolved = tmp_path.resolve()
    argv = container_argv(
        ContainerLimits(), image="python:3.12-slim", command=("pytest", "-q"), root=tmp_path
    )
    joined = " ".join(argv)

    # 1. no egress, 2. immutable rootfs, 3. non-root, 4. no capabilities,
    # 5. no privilege escalation, 6. memory bounded including swap.
    assert "--network none" in joined
    assert "--read-only" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--memory-swap") + 1] == "512m"
    assert argv[argv.index("--volume") + 1] == f"{resolved}:{CONTAINER_WORKDIR}:rw"
    assert argv[argv.index("--workdir") + 1] == CONTAINER_WORKDIR
    # The command is an argv vector: an argument stays a word even when it looks like shell.
    hostile = container_argv(
        ContainerLimits(), image="alpine", command=("rm", "-rf", "/; echo pwned"), root=tmp_path
    )
    assert hostile[-1] == "/; echo pwned"
    assert "sh" not in hostile[: hostile.index("alpine")]


def test_container_limits_refuse_root_and_unbounded_swap() -> None:
    with pytest.raises(ValueError, match="must not run as root"):
        ContainerLimits(user="0:0")
    assert ContainerLimits(memory="1g").memory_swap == "1g"
    with pytest.raises(ValueError):
        ContainerLimits(memory="lots")


def test_container_argv_requires_an_image_command_and_canonical_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="image"):
        container_argv(ContainerLimits(), image="", command=("ls",), root=tmp_path)
    with pytest.raises(ValueError, match="command"):
        container_argv(ContainerLimits(), image="alpine", command=(), root=tmp_path)
    with pytest.raises(FileNotFoundError):
        container_argv(ContainerLimits(), image="alpine", command=("ls",), root=tmp_path / "absent")


async def test_container_backend_runs_the_command_it_was_asked_for(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...]) -> CommandResult:
        seen.append(argv)
        return CommandResult(exit_code=0, output="ok", truncated=False)

    backend = ContainerBackend(tmp_path, image="python:3.12-slim", runner=runner)
    result = await backend.run(("pytest", "-q"))

    assert result.output == "ok"
    assert seen[0][:3] == ("docker", "run", "--rm")
    assert seen[0][-2:] == ("pytest", "-q")
    assert backend.name == "container"


async def test_container_backend_bounds_its_output(tmp_path: Path) -> None:
    async def runner(argv: tuple[str, ...]) -> CommandResult:
        return CommandResult(exit_code=0, output="x" * 200, truncated=False)

    backend = ContainerBackend(tmp_path, image="alpine", runner=runner)
    result = await backend.run(("cat", "big"), max_output_bytes=50)

    assert result.truncated is True
    assert result.output.endswith("[tool output truncated]")


async def test_a_timed_out_command_is_discarded_not_reused(tmp_path: Path) -> None:
    started: list[tuple[str, ...]] = []
    executed: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...]) -> CommandResult:
        if argv[1] == "run":
            started.append(argv)
            return CommandResult(exit_code=0, output="container-1\n", truncated=False)
        executed.append(argv)
        if len(executed) == 1:
            return CommandResult(
                exit_code=124, output="[container command timed out]", truncated=False
            )
        return CommandResult(exit_code=0, output="ok", truncated=False)

    pool = WarmContainerPool(
        limits=ContainerLimits(), image="alpine", root=tmp_path, size=1, runner=runner
    )
    await pool.warm()
    assert pool.live_containers == ("container-1",)
    assert "sleep" in started[0]

    timed_out = await pool.run(("pytest",))
    assert timed_out.exit_code == 124
    assert pool.live_containers == ()
    # A discarded container is replaced rather than handed on with an unknown process tree.
    second = await pool.run(("pytest",))
    assert second.exit_code == 0
    assert len(started) == 2
    await pool.aclose()
    assert pool.live_containers == ()


async def test_pool_close_is_idempotent_and_removes_every_container(tmp_path: Path) -> None:
    removals: list[tuple[str, ...]] = []
    starts = iter(range(10))

    async def runner(argv: tuple[str, ...]) -> CommandResult:
        if argv[1] == "run":
            return CommandResult(exit_code=0, output=f"id-{next(starts)}\n", truncated=False)
        if argv[1] == "rm":
            removals.append(argv)
        return CommandResult(exit_code=0, output="", truncated=False)

    pool = WarmContainerPool(
        limits=ContainerLimits(), image="alpine", root=tmp_path, size=2, runner=runner
    )
    await pool.warm()
    assert len(pool.live_containers) == 2

    await pool.aclose()
    await pool.aclose()

    assert len(removals) == 2
    assert pool.live_containers == ()


def test_exec_argv_names_the_container_and_workdir() -> None:
    assert exec_argv("abc", ("ls",)) == (
        "docker",
        "exec",
        "--workdir",
        CONTAINER_WORKDIR,
        "abc",
        "ls",
    )
    with pytest.raises(ValueError, match="container id"):
        exec_argv("", ("ls",))


def test_pooled_argv_is_detached(tmp_path: Path) -> None:
    argv = pooled_argv(
        ContainerLimits(), image="alpine", root=tmp_path, name="box", command=("sleep", "infinity")
    )

    assert argv[:5] == ("docker", "run", "--detach", "--rm", "--name")
    assert argv.index("--network") > 0


# --- composite routing -------------------------------------------------------


def test_composite_routes_by_path_prefix_and_defaults_the_rest(tmp_path: Path) -> None:
    calls: list[str] = []

    async def runner(argv: tuple[str, ...]) -> CommandResult:
        calls.append(argv[-1])
        return CommandResult(exit_code=0, output="", truncated=False)

    seed(tmp_path)
    sandbox = ContainerBackend(tmp_path, image="alpine", runner=runner)
    composite = CompositeBackend(
        routes=(("docs", sandbox),),
        default=LocalBackend(tmp_path),
    )

    assert composite.backend_for("docs/guide.md").name == "container"
    assert composite.backend_for("docs").name == "container"
    # A sibling directory that merely shares the prefix must not be routed.
    assert composite.backend_for("docsx/guide.md").name == "local"
    assert composite.backend_for("src/service.py").name == "local"
    assert composite.routes == (("docs", "container"),)
    assert composite.backend_for("docs/guide.md").list_files() == sandbox.list_files()

    factory = composite_factory(routes=(("docs", sandbox),), default=LocalBackend)
    assert factory(tmp_path).backend_for("docs/x.md").name == "container"


async def test_commands_always_use_the_default_backend(tmp_path: Path) -> None:
    """Commands are not path-scoped: a test run touches the whole workspace."""
    seen: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...]) -> CommandResult:
        seen.append(argv)
        return CommandResult(exit_code=0, output="", truncated=False)

    seed(tmp_path)
    composite = CompositeBackend(
        routes=(("docs", ContainerBackend(tmp_path, image="alpine", runner=runner)),),
        default=LocalBackend(tmp_path),
    )

    await composite.run(("sh", "-c", "true"))

    assert seen == []
    assert composite.name == "composite"


def test_container_backend_factory_is_callable_per_workspace(tmp_path: Path) -> None:
    factory = container_backend_factory(image="alpine")
    backend = factory(tmp_path)

    assert isinstance(backend, ContainerBackend)
    assert backend.name == "container"
    # A backend that starts containers must be closable; without a pool there is
    # nothing to close, and constructing one must not require a daemon.
    assert asyncio.iscoroutinefunction(backend.run)


async def test_a_failed_pool_start_is_a_backend_error(tmp_path: Path) -> None:
    async def runner(argv: tuple[str, ...]) -> CommandResult:
        return CommandResult(exit_code=125, output="daemon not running", truncated=False)

    pool = WarmContainerPool(limits=ContainerLimits(), image="alpine", root=tmp_path, runner=runner)

    with pytest.raises(BackendError, match="could not start sandbox container"):
        await pool.warm()


def test_public_file_iterator_closes_held_directories_when_stopped_early(tmp_path, monkeypatch):
    import os

    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/first.txt").write_text("first")
    (tmp_path / "nested/second.txt").write_text("second")
    backend = LocalBackend(tmp_path)
    opened: set[int] = set()
    original_open, original_close = os.open, os.close

    def open_tracked(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def close_tracked(descriptor):
        original_close(descriptor)
        opened.discard(descriptor)

    monkeypatch.setattr(os, "open", open_tracked)
    monkeypatch.setattr(os, "close", close_tracked)
    files = backend.iter_files()
    try:
        assert next(files).path == "nested/first.txt"
        assert opened
    finally:
        files.close()
    assert not opened


def test_public_file_iterator_stays_scoped_and_preserves_default_skips(tmp_path):
    seed(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path / "src", target_is_directory=True)
    paths = [entry.path for entry in LocalBackend(tmp_path).iter_files()]
    assert paths == ["src/service.py"]

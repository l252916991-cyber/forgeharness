"""Edge branches across the backends, the spill store and the skill installer.

These are the branches a happy-path suite never reaches: refusals, limits, and the
"declared nothing" cases. They are cheap to test and they are the ones that decide
whether a boundary holds when an operator misconfigures it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest

from forgeharness.backends.composite import CompositeBackend
from forgeharness.backends.container import (
    ContainerBackend,
    ContainerLimits,
    WarmContainerPool,
    container_argv,
    exec_argv,
)
from forgeharness.backends.local import LocalBackend
from forgeharness.backends.protocol import BackendError, NotRegularFile
from forgeharness.context.spill import BlobStore, MessageSpill, SpillCorrupt
from forgeharness.domain.models import (
    Actor,
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ResourceScope,
    RunContext,
)
from forgeharness.models.caching import (
    CacheDirective,
    CacheMode,
    CachePlan,
    render_payload,
)
from forgeharness.models.prepared import canonical_json
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.skills.install import (
    InstallPolicy,
    SkillInstaller,
    SkillInstallError,
    fetch_with_urllib,
)
from forgeharness.skills.loader import SkillLoader, SkillLoadError
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.process import CommandResult
from forgeharness.tools.registry import ToolRegistry


def ok(output: str = "", exit_code: int = 0) -> CommandResult:
    return CommandResult(exit_code=exit_code, output=output, truncated=False)


# --------------------------------------------------------------------------- container


def test_limits_default_swap_to_the_memory_cap() -> None:
    limits = ContainerLimits(memory="256m")
    assert limits.memory_swap == "256m"


def test_an_explicit_swap_cap_survives_the_default() -> None:
    limits = ContainerLimits(memory="256m", memory_swap="768m")
    assert limits.memory_swap == "768m"


def test_optional_hardening_flags_can_be_turned_off() -> None:
    argv = container_argv(
        ContainerLimits(read_only_rootfs=False, no_new_privileges=False),
        image="python:3.12-slim",
        command=("true",),
        root=Path.cwd(),
    )
    assert "--read-only" not in argv
    assert "no-new-privileges" not in " ".join(argv)


def test_a_named_container_gets_its_name_flag() -> None:
    argv = container_argv(
        ContainerLimits(), image="python:3.12-slim", command=("true",), root=Path.cwd(), name="job"
    )
    assert argv[argv.index("--name") + 1] == "job"


def test_exec_refuses_an_empty_command() -> None:
    with pytest.raises(ValueError, match="command must not be empty"):
        exec_argv("abc123", ())


def test_the_pool_refuses_a_zero_size() -> None:
    with pytest.raises(ValueError, match="pool size must be at least 1"):
        WarmContainerPool(limits=ContainerLimits(), image="img", root=Path.cwd(), size=0)


async def test_a_container_without_an_id_is_not_adopted(tmp_path: Path) -> None:
    async def runner(_argv: tuple[str, ...]) -> CommandResult:
        return ok("   \n")

    pool = WarmContainerPool(limits=ContainerLimits(), image="img", root=tmp_path, runner=runner)
    with pytest.raises(BackendError, match="did not report a container id"):
        await pool.warm()


async def test_a_pool_backed_backend_routes_runs_through_the_pool(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...]) -> CommandResult:
        seen.append(argv)
        if argv[:2] == ("docker", "run"):
            return ok("cid-1")
        return ok("pooled")

    pool = WarmContainerPool(limits=ContainerLimits(), image="img", root=tmp_path, runner=runner)
    await pool.warm()
    backend = ContainerBackend(tmp_path, image="img", pool=pool)

    result = await backend.run(("pytest", "-q"))

    assert result.output == "pooled"
    assert seen[-1][:2] == ("docker", "exec")
    await pool.aclose()


async def test_a_container_dropped_mid_command_is_not_resurrected(tmp_path: Path) -> None:
    pool: WarmContainerPool | None = None

    async def runner(argv: tuple[str, ...]) -> CommandResult:
        if argv[:2] == ("docker", "run"):
            return ok("cid-1")
        assert pool is not None
        if argv[:2] == ("docker", "rm"):
            return ok()
        # Stands in for a close that happened while the command was in flight: the
        # container is no longer owned, so the next caller must not get it back.
        await pool._discard("cid-1")
        return ok("done")

    pool = WarmContainerPool(limits=ContainerLimits(), image="img", root=tmp_path, runner=runner)
    await pool.warm()

    result = await pool.run(("true",))

    assert result.output == "done"
    assert pool.live_containers == ()


# ------------------------------------------------------------------------------ local


def test_listing_a_file_is_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x")
    with pytest.raises(NotRegularFile, match="not a directory"):
        LocalBackend(tmp_path).list_files(path="app.py")


def test_listing_stops_one_entry_past_the_limit(tmp_path: Path) -> None:
    for index in range(4):
        (tmp_path / f"f{index}.txt").write_text("x")

    entries = LocalBackend(tmp_path).list_files(limit=2)

    # One extra so the caller can say "truncated" without a second walk.
    assert len(entries) == 3


def test_writing_through_a_symlink_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")

    with pytest.raises(ValueError, match="refusing to replace a symlink"):
        LocalBackend(tmp_path).write_text("link.txt", "y")


def test_reading_a_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    with pytest.raises(NotRegularFile, match="not a regular file"):
        LocalBackend(tmp_path).read_text("pkg")


async def test_running_an_empty_vector_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="command must not be empty"):
        await LocalBackend(tmp_path).run(())


def test_a_composite_needs_at_least_one_route(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one route"):
        CompositeBackend(routes=(), default=LocalBackend(tmp_path))


# ------------------------------------------------------------------------------ spill


def test_an_unknown_prefix_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported blob prefix"):
        BlobStore(tmp_path, prefix="nope")  # type: ignore[arg-type]


def test_blob_reads_validate_their_window(tmp_path: Path) -> None:
    store = BlobStore(tmp_path, prefix="ctx")
    reference = store.put("one\ntwo\n")

    assert store.read(reference, limit=3) == "one"
    with pytest.raises(ValueError, match="offset must be nonnegative"):
        store.read(reference, offset=-1)
    with pytest.raises(ValueError, match="limit must be nonnegative"):
        store.read(reference, limit=-1)


def test_a_tampered_blob_is_reported(tmp_path: Path) -> None:
    store = BlobStore(tmp_path, prefix="ctx")
    reference = store.put("payload\n")
    (tmp_path / f"{store.resolve(reference)[0].name}").write_text("payload!\n")

    with pytest.raises(SpillCorrupt):
        store.read(reference)


def test_an_empty_history_stores_an_empty_blob(tmp_path: Path) -> None:
    spill = MessageSpill(tmp_path)
    reference = spill.store(())

    assert spill.count(reference) == 0
    assert spill.read(reference).startswith("[no stored messages at offset")


def test_message_reads_validate_their_window(tmp_path: Path) -> None:
    spill = MessageSpill(tmp_path)
    reference = spill.store((Message(role=MessageRole.USER, content="hi"),))

    with pytest.raises(ValueError, match="offset must be nonnegative"):
        spill.read(reference, offset=-1)
    with pytest.raises(ValueError, match="limit must be nonnegative"):
        spill.read(reference, limit=-1)
    assert spill.read(reference, offset=5).startswith("[no stored messages at offset 5")


# ------------------------------------------------------------------------------ cache


def test_an_empty_plan_has_no_cached_fraction() -> None:
    plan = CachePlan(
        mode=CacheMode.PREFIX,
        cacheable=True,
        prefix_messages=0,
        prefix_tokens=0,
        total_tokens=0,
        directives=(),
    )
    assert plan.cached_fraction == 0.0
    assert plan.worth_marking is False


def test_render_skips_directives_it_cannot_satisfy() -> None:
    class Prepared:
        payload_json = '{"messages": [], "model": "m"}'
        payload: ClassVar[dict[str, object]] = {"messages": [], "model": "m"}
        format = "chat-completions-v1"

    plan = CachePlan(
        mode=CacheMode.EXPLICIT,
        cacheable=True,
        prefix_messages=0,
        prefix_tokens=0,
        total_tokens=1,
        directives=(
            CacheDirective(target="tools"),
            CacheDirective(target="system", message_index=7),
        ),
    )

    # No tools and no message 7: nothing to mark, so the payload is returned untouched.
    assert render_payload(Prepared(), plan) == canonical_json(Prepared.payload)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------ skills


def _package(tools: str = "read_file", smoke: tuple[str, ...] | None = None) -> str:
    head = (
        "---\n"
        "name: helper-skill\n"
        "version: 1.0.0\n"
        "description: Helps with a narrow task\n"
        f"allowed_tools:\n  - {tools}\n"
    )
    if smoke is not None:
        head += "smoke_command:\n" + "".join(f'  - "{part}"\n' for part in smoke)
    return head + "---\n\nDo the narrow task twice.\n"


def _passing_backend(root: Path) -> LocalBackend:
    backend = LocalBackend(root)

    async def run(command: tuple[str, ...], *, max_output_bytes: int = 50_000) -> CommandResult:
        return ok("ok")

    backend.run = run  # type: ignore[method-assign]
    return backend


def _installer(**policy: object) -> SkillInstaller:
    values: dict[str, object] = {"allow_local_sources": True}
    values.update(policy)
    return SkillInstaller(
        available_tools={"read_file", "write_file"},
        policy=InstallPolicy(**values),  # type: ignore[arg-type]
    )


async def test_a_package_that_loses_nothing_records_no_trim(tmp_path: Path) -> None:
    source = tmp_path / "candidate.md"
    source.write_text(_package(smoke=("python3", "-c", "print(1)")))
    events: list[str] = []

    class Recorder:
        def append(self, event: str, payload: dict[str, object]) -> None:
            events.append(event)

    installed = await SkillInstaller(
        available_tools={"read_file", "write_file"},
        policy=InstallPolicy(allow_local_sources=True),
        backend=_passing_backend,
        trace=Recorder(),
    ).install(str(source), destination=tmp_path / "skills")

    assert installed.dropped_tools == ()
    assert "skill.trimmed" not in events


async def test_a_smoke_command_may_be_optional(tmp_path: Path) -> None:
    source = tmp_path / "candidate.md"
    source.write_text(_package())

    installed = await _installer(require_smoke=False).install(
        str(source), destination=tmp_path / "skills"
    )

    assert installed.smoke.ran is False
    assert installed.smoke.backend == ""


async def test_a_timed_out_smoke_command_fails_the_install(tmp_path: Path) -> None:
    source = tmp_path / "candidate.md"
    source.write_text(_package(smoke=("python3", "smoke.py")))

    def backend(root: Path) -> LocalBackend:
        inner = LocalBackend(root)

        async def run(command: tuple[str, ...], *, max_output_bytes: int = 50_000) -> CommandResult:
            raise TimeoutError("too slow")

        inner.run = run  # type: ignore[method-assign]
        return inner

    with pytest.raises(SkillInstallError) as failure:
        await SkillInstaller(
            available_tools={"read_file"},
            policy=InstallPolicy(allow_local_sources=True),
            backend=backend,
        ).install(str(source), destination=tmp_path / "skills")

    assert failure.value.code == "smoke_failed"


async def test_a_directory_without_a_manifest_is_not_upgradable(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    (destination / "helper-skill").mkdir(parents=True)
    source = tmp_path / "candidate.md"
    source.write_text(_package(smoke=("python3", "smoke.py")))

    with pytest.raises(SkillInstallError) as failure:
        await SkillInstaller(
            available_tools={"read_file"},
            policy=InstallPolicy(allow_local_sources=True),
            backend=_passing_backend,
        ).install(str(source), destination=destination)

    assert failure.value.code == "already_installed"


async def test_a_fetch_that_exceeds_the_limit_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "candidate.md"
    source.write_text(_package(smoke=("python3", "smoke.py")) + "x" * 5_000)

    with pytest.raises(SkillInstallError) as failure:
        await _installer(max_bytes=1_500).install(str(source), destination=tmp_path / "skills")

    assert failure.value.code == "too_large"


def test_loader_rejects_a_missing_manifest_and_duplicate_names(tmp_path: Path) -> None:
    loader = SkillLoader()
    iffy = tmp_path / "iffy"
    iffy.mkdir()
    (iffy / "README.md").write_text("no manifest here")
    with pytest.raises(SkillLoadError, match=r"non\-symlink SKILL\.md"):
        loader.load(iffy / "README.md")

    with pytest.raises(FileNotFoundError):
        loader.load_directory(tmp_path / "absent")

    for name in ("a", "b"):
        target = tmp_path / name
        target.mkdir()
        (target / "SKILL.md").write_text(
            _package(smoke=("python3", "smoke.py")).replace("helper-skill", "same")
        )
    with pytest.raises(SkillLoadError, match="unique"):
        loader.load_directory(tmp_path)


def test_loaded_skill_reports_no_unavailable_tools_when_nothing_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(_package(smoke=("python3", "smoke.py")))
    loaded = SkillLoader().load(path)

    assert loaded.unavailable_tools({"read_file"}) == ()
    assert loaded.trimmed({"read_file"}).manifest.allowed_tools == ("read_file",)


# ------------------------------------------------------------------------- install


async def test_a_fetch_that_never_answers_is_reported_as_a_fetch_failure() -> None:
    async def slow(_source: str, _policy: InstallPolicy) -> bytes:
        await asyncio.sleep(1)
        return b"too late"

    installer = SkillInstaller(
        available_tools={"read_file"},
        policy=InstallPolicy(allow_local_sources=True, fetch_timeout_seconds=0.01),
        fetcher=slow,
    )

    with pytest.raises(SkillInstallError) as failure:
        await installer.install("memory://skill", destination=Path("/tmp/unused-skills"))

    assert failure.value.code == "fetch_failed"


def test_urllib_fetch_maps_local_failures_and_https_refusals() -> None:
    policy = InstallPolicy(allow_local_sources=True, fetch_timeout_seconds=1)

    with pytest.raises(SkillInstallError) as missing:
        fetch_with_urllib("/nonexistent/SKILL.md", policy)
    assert missing.value.code == "fetch_failed"

    with pytest.raises(SkillInstallError) as insecure:
        fetch_with_urllib("http://example.invalid/SKILL.md", InstallPolicy())
    assert insecure.value.code == "source_rejected"

    with pytest.raises(SkillInstallError) as unreachable:
        fetch_with_urllib("https://127.0.0.1:1/SKILL.md", policy)
    assert unreachable.value.code == "fetch_failed"


# ---------------------------------------------------------------------------- local


def test_writing_over_a_directory_from_the_backend_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    with pytest.raises(NotRegularFile, match="not a regular file"):
        LocalBackend(tmp_path).write_text("pkg", "x")


# ---------------------------------------------------------------------------- runtime


async def test_a_workspace_that_contradicts_the_run_context_is_refused(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    context = RunContext(
        application_id="fixture",
        application_version="1",
        policy_version="1",
        run_id="r",
        session_id="s",
        correlation_id="c",
        actor=Actor(id="user"),
        scope=ResourceScope(tenant_id="t", workspace=scoped),
    )
    registry = ToolRegistry()
    runtime = AgentRuntime(
        model=_Finish(),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
    )

    with pytest.raises(ValueError, match="workspace does not match trusted scope"):
        await runtime.run(
            task_id="task", task="read", workspace=tmp_path / "elsewhere", run_context=context
        )


def test_a_negative_todo_rejection_budget_is_refused() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="max_todo_rejections"):
        AgentRuntime(
            model=_Finish(),
            registry=registry,
            dispatcher=ToolDispatcher(registry),
            policy=_AllowAll(),
            trace=InMemoryTrace("task"),
            max_todo_rejections=-1,
        )


class _Finish:
    async def decide(self, request: object) -> ModelResult:
        del request
        return ModelResult(action=FinalAction(content="done"))


class _AllowAll:
    def evaluate(self, *, task_id: str, spec: object, call: object) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test")

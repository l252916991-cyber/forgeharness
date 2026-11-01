"""Tests for the skill install pipeline: fetch, validate, trim, smoke, promote."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request

import pytest

from forgeharness.backends.local import LocalBackend
from forgeharness.skills.install import (
    InstallPolicy,
    SkillInstaller,
    SkillInstallError,
    _PolicyRedirects,
    _promotion_lock,
    _promotion_root,
    fetch_with_urllib,
)
from forgeharness.skills.loader import SkillLoader
from forgeharness.tools.process import CommandResult

PACKAGE = """---
name: invoice-review
version: 1.0.0
description: Review invoice changes for rounding errors
allowed_tools:
  - read_file
  - run_tests
  - audit_ledger
smoke_command:
  - python3
  - -c
  - import pathlib, sys; sys.exit(0 if pathlib.Path('SKILL.md').is_file() else 1)
---

Check every currency total twice before reporting.
"""


def installer(**kwargs: object) -> SkillInstaller:
    values: dict[str, object] = {
        "available_tools": {"read_file", "run_tests", "write_file"},
        "policy": InstallPolicy(allow_local_sources=True),
        # These local fixture packages are deliberately trusted by the test host.
        "backend": LocalBackend,
    }
    values.update(kwargs)
    return SkillInstaller(**values)  # type: ignore[arg-type]


def source(tmp_path: Path, body: str = PACKAGE) -> Path:
    path = tmp_path / "candidate.md"
    path.write_text(body)
    return path


async def test_install_promotes_a_package_and_records_the_dropped_tools(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    installed = await installer().install(str(source(tmp_path)), destination=destination)

    assert installed.manifest.name == "invoice-review"
    assert installed.dropped_tools == ("audit_ledger",)
    assert installed.smoke.ran is True
    assert installed.smoke.exit_code == 0
    assert installed.smoke.backend == "local"
    assert installed.path == str(destination / "invoice-review")

    # What landed on disk is the trimmed manifest, so a later load cannot resurrect a
    # capability this deployment never had.
    on_disk = (destination / "invoice-review" / "SKILL.md").read_text()
    assert "audit_ledger" not in on_disk
    assert "smoke_command" in on_disk
    loaded = SkillLoader().load(destination / "invoice-review" / "SKILL.md")
    assert loaded.manifest.allowed_tools == ("read_file", "run_tests")
    assert "Check every currency total twice" in loaded.instructions
    assert installed.to_loaded().manifest == loaded.manifest


async def test_install_is_idempotent_for_identical_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    first = await installer().install(str(source(tmp_path)), destination=destination)
    second = await installer().install(str(source(tmp_path)), destination=destination)

    assert first.path == second.path
    assert first.digest == second.digest


async def test_install_refuses_to_replace_different_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    await installer().install(str(source(tmp_path)), destination=destination)
    changed = source(tmp_path, PACKAGE.replace("twice", "three times"))

    with pytest.raises(SkillInstallError) as failure:
        await installer().install(str(changed), destination=destination)
    assert failure.value.code == "already_installed"

    # An explicit upgrade policy is how a deployment opts into changing run behaviour.
    upgraded = await installer(
        policy=InstallPolicy(allow_local_sources=True, allow_upgrade=True)
    ).install(str(changed), destination=destination)
    assert "three times" in upgraded.instructions


async def test_a_package_without_a_smoke_command_proves_nothing(tmp_path: Path) -> None:
    body = PACKAGE.split("smoke_command:")[0] + "\n---\n\nCheck every total twice.\n"
    with pytest.raises(SkillInstallError) as failure:
        await installer().install(str(source(tmp_path, body)), destination=tmp_path / "skills")

    assert failure.value.code == "smoke_missing"


async def test_a_failing_smoke_command_blocks_promotion(tmp_path: Path) -> None:
    body = PACKAGE.replace("sys.exit(0 if", "sys.exit(1 if")
    destination = tmp_path / "skills"

    with pytest.raises(SkillInstallError) as failure:
        await installer().install(str(source(tmp_path, body)), destination=destination)
    assert failure.value.code == "smoke_failed"
    assert not destination.exists()


async def test_the_smoke_test_runs_in_the_configured_backend(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    def backend(root: Path) -> LocalBackend:
        inner = LocalBackend(root)

        async def run(command: tuple[str, ...], *, max_output_bytes: int = 50_000) -> CommandResult:
            seen.append(command)
            return CommandResult(exit_code=0, output="smoked", truncated=False)

        inner.run = run  # type: ignore[method-assign]
        return inner

    installed = await installer(backend=backend).install(
        str(source(tmp_path)), destination=tmp_path / "skills"
    )

    assert len(seen) == 1
    assert seen[0][:2] == ("python3", "-c")
    assert installed.smoke.output == "smoked"


async def test_a_malformed_package_is_rejected_before_anything_is_written(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    body = PACKAGE.replace("name: invoice-review", "name: Invoice Review")

    with pytest.raises(SkillInstallError) as failure:
        await installer().install(str(source(tmp_path, body)), destination=destination)

    assert failure.value.code == "invalid_package"
    assert not destination.exists()


async def test_oversized_and_disallowed_sources_are_refused(tmp_path: Path) -> None:
    policy = InstallPolicy(max_bytes=1_024)
    with pytest.raises(SkillInstallError) as too_large:
        fetch_with_urllib(str(source(tmp_path, PACKAGE + "x" * 2_000)), policy)
    assert too_large.value.code == "too_large"

    with pytest.raises(SkillInstallError) as rejected:
        fetch_with_urllib(
            "http://example.com/SKILL.md", InstallPolicy(allowed_hosts=("example.com",))
        )
    assert rejected.value.code == "source_rejected"

    with pytest.raises(SkillInstallError) as local_blocked:
        fetch_with_urllib(str(source(tmp_path)), InstallPolicy(allow_local_sources=False))
    assert local_blocked.value.code == "source_rejected"

    with pytest.raises(SkillInstallError) as host_blocked:
        fetch_with_urllib(
            "https://evil.test/SKILL.md", InstallPolicy(allowed_hosts=("example.com",))
        )
    assert host_blocked.value.code == "source_rejected"


async def test_install_records_every_stage_in_the_trace(tmp_path: Path) -> None:
    events: list[str] = []

    class Recorder:
        def append(self, event: str, payload: dict[str, object]) -> None:
            events.append(event)

    await installer(trace=Recorder()).install(
        str(source(tmp_path)), destination=tmp_path / "skills"
    )

    assert events == [
        "skill.fetched",
        "skill.validated",
        "skill.trimmed",
        "skill.smoked",
        "skill.promoted",
    ]


async def test_a_failed_fetch_is_reported_as_such(tmp_path: Path) -> None:
    with pytest.raises(SkillInstallError) as failure:
        await installer().install(str(tmp_path / "absent.md"), destination=tmp_path / "skills")
    assert failure.value.code == "fetch_failed"


async def test_unconfigured_smoke_backend_never_runs_a_package_command(tmp_path: Path) -> None:
    marker = tmp_path / "host-command-ran"
    body = PACKAGE.replace(
        "import pathlib, sys; sys.exit(0 if pathlib.Path('SKILL.md').is_file() else 1)",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')",
    )
    destination = tmp_path / "skills"
    unconfigured = SkillInstaller(available_tools=set())

    with pytest.raises(SkillInstallError) as failure:
        await unconfigured.install(str(source(tmp_path, body)), destination=destination)

    assert failure.value.code == "smoke_backend_required"
    assert not marker.exists() and not destination.exists()


@pytest.mark.parametrize(
    "target",
    [
        "https://blocked.test/SKILL.md",
        "http://allowed.test/SKILL.md",
        "file:///etc/passwd",
        "ftp://allowed.test/SKILL.md",
        "https://user:password@allowed.test/SKILL.md",
    ],
)
def test_redirects_revalidate_scheme_host_and_authority_before_opening(target: str) -> None:
    handler = _PolicyRedirects(InstallPolicy(allowed_hosts=("allowed.test",)))
    request = Request("https://allowed.test/SKILL.md")

    with pytest.raises(SkillInstallError) as failure:
        handler.redirect_request(request, None, 302, "Found", {}, target)

    assert failure.value.code == "source_rejected"
    assert handler.hops == 0


def test_allowed_redirects_have_an_explicit_total_hop_limit() -> None:
    handler = _PolicyRedirects(InstallPolicy(allowed_hosts=("allowed.test",), max_redirects=1))
    request = Request("https://allowed.test/first")
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://allowed.test/second"
    )
    assert redirected.full_url == "https://allowed.test/second"

    with pytest.raises(SkillInstallError, match="redirect limit"):
        handler.redirect_request(redirected, None, 302, "Found", {}, "https://allowed.test/third")


@pytest.mark.parametrize("source_url", ["ftp://example.test/a", "data:text/plain,text"])
def test_disabling_https_does_not_enable_arbitrary_url_schemes(source_url: str) -> None:
    with pytest.raises(SkillInstallError) as failure:
        fetch_with_urllib(source_url, InstallPolicy(require_https=False))
    assert failure.value.code == "source_rejected"


def test_real_http_redirect_cannot_escape_the_configured_host_allowlist() -> None:
    requests: list[str] = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/blocked")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(SkillInstallError) as failure:
            fetch_with_urllib(
                f"http://127.0.0.1:{server.server_port}/initial",
                InstallPolicy(require_https=False, allowed_hosts=("127.0.0.1",)),
            )
        assert failure.value.code == "source_rejected"
        assert requests == ["/initial"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_failed_upgrade_restores_the_complete_previous_package(tmp_path: Path, monkeypatch):
    destination = tmp_path / "skills"
    installed = await installer().install(str(source(tmp_path)), destination=destination)
    old = Path(installed.path) / "SKILL.md"
    original = old.read_bytes()
    companion = old.parent / "operator-data.txt"
    companion.write_text("keep this operator file")
    changed = source(tmp_path, PACKAGE.replace("twice", "three times"))
    import os

    replace = os.replace

    def fail_promotion(src, dst, **kwargs):
        if str(src).startswith(".promote-"):
            raise OSError("injected rename failure")
        return replace(src, dst, **kwargs)

    monkeypatch.setattr("forgeharness.skills.install.os.replace", fail_promotion)
    with pytest.raises(OSError, match="injected rename"):
        await installer(policy=InstallPolicy(allow_upgrade=True)).install(
            str(changed), destination=destination
        )

    assert old.read_bytes() == original
    assert companion.read_text() == "keep this operator file"
    assert not list(destination.glob(".backup-*"))
    assert not list(destination.glob(".promote-*"))


async def test_a_concurrent_writer_cannot_replace_the_locked_package(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    original = await installer().install(str(source(tmp_path)), destination=destination)
    installed_file = Path(original.path) / "SKILL.md"
    previous = installed_file.read_bytes()
    changed = source(tmp_path, PACKAGE.replace("twice", "three times"))

    with _promotion_root(destination) as directory, _promotion_lock(directory, "invoice-review"):
        with pytest.raises(SkillInstallError) as failure:
            await installer(policy=InstallPolicy(allow_upgrade=True)).install(
                str(changed), destination=destination
            )

    assert failure.value.code == "install_conflict"
    assert installed_file.read_bytes() == previous


@pytest.mark.parametrize("component", ["destination", "parent", "package", "manifest", "lock"])
async def test_promotion_never_follows_a_symlink(tmp_path: Path, component: str) -> None:
    destination = tmp_path / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "destination":
        destination.symlink_to(outside, target_is_directory=True)
    elif component == "parent":
        (tmp_path / "alias").symlink_to(outside, target_is_directory=True)
        destination = tmp_path / "alias" / "skills"
    else:
        destination.mkdir()
        if component == "package":
            (destination / "invoice-review").symlink_to(outside, target_is_directory=True)
        elif component == "manifest":
            package = destination / "invoice-review"
            package.mkdir()
            (outside / "existing.md").write_text("operator content")
            (package / "SKILL.md").symlink_to(outside / "existing.md")
        else:
            (outside / "lock").write_text("operator lock")
            (destination / ".invoice-review.lock").symlink_to(outside / "lock")
    before = {str(path): path.read_bytes() for path in outside.rglob("*") if path.is_file()}

    with pytest.raises(SkillInstallError) as failure:
        await installer(policy=InstallPolicy(allow_upgrade=True)).install(
            str(source(tmp_path)), destination=destination
        )

    assert failure.value.code in {"destination_rejected", "install_conflict"}
    assert {str(path): path.read_bytes() for path in outside.rglob("*") if path.is_file()} == before
    assert not (outside / "SKILL.md").exists()


async def test_hidden_staging_and_backup_directories_are_never_loaded(tmp_path: Path) -> None:
    for name in (".promote-staged", ".backup-invoice-review-old"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(PACKAGE)
    assert SkillLoader().load_directory(tmp_path) == ()


async def test_manifest_values_roundtrip_with_literal_newlines(tmp_path: Path) -> None:
    body = PACKAGE.replace(
        "description: Review invoice changes for rounding errors",
        'description: "Review invoice changes\\nfor rounding errors"',
    )
    installed = await installer().install(
        str(source(tmp_path, body)), destination=tmp_path / "skills"
    )
    loaded = SkillLoader().load(Path(installed.path) / "SKILL.md")
    assert loaded.manifest == installed.manifest
    assert "\n" in loaded.manifest.description


async def test_source_query_fragment_and_credentials_never_enter_the_trace(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    source_url = "https://operator:private-password@skills.test/SKILL.md?token=private-token#secret"

    class Recorder:
        def append(self, event: str, payload: dict[str, object]) -> None:
            events.append(payload)

    async def fetcher(source: str, policy: InstallPolicy) -> bytes:
        assert source == source_url
        return PACKAGE.encode()

    await installer(trace=Recorder(), fetcher=fetcher).install(
        source_url, destination=tmp_path / "skills"
    )

    import hashlib

    for event in events:
        encoded = str(event)
        assert "private-password" not in encoded
        assert "private-token" not in encoded
        if "source" in event:
            assert event["source"] == "https://skills.test/SKILL.md"
            assert event["source_sha256"] == hashlib.sha256(source_url.encode()).hexdigest()

"""Install a Skill package: fetch, validate, trim, smoke-test, then promote.

The loader already refuses a malformed package, a symlink entry point, and a capability
the deployment does not compose. What it cannot do is answer "does this skill actually
work here", and that is the question a downloaded package raises. A skill is *instructions
that change what every later run does*, so admitting one without evidence is how a
harness ends up with a permanently wrong system prompt that no test covers.

The pipeline is five stages, and each one exists because skipping it has a named failure:

``stage``
    Fetch into a scratch directory, bounded in bytes and redirected-hop count. Writing
    straight into the skills root would let a half-downloaded package be loaded by the
    next run.
``validate``
    The strict loader parses it. A package whose manifest does not round-trip is not a
    skill, it is text that looks like one.
``trim``
    Capabilities this deployment does not compose are removed and *recorded*. Rejecting
    instead would force every deployment to maintain a fork of the same package.
``smoke``
    The declared ``smoke_command`` runs in a backend -- in practice a container, so a
    hostile package's first action cannot be an action on the host. A non-zero exit or a
    missing smoke command fails the install.
``promote``
    Move the staged directory into place atomically. An existing package with a different
    digest is refused rather than overwritten: a silent upgrade changes run behaviour
    without changing any input the evaluation recorded.

Failures are typed so the caller can distinguish "this package is bad" from "this
deployment is offline", instead of catching ``Exception`` and guessing.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import Field

from forgeharness.backends.protocol import BackendFactory
from forgeharness.domain.models import FrozenModel
from forgeharness.sdk.privacy import CredentialMask
from forgeharness.skills.loader import LoadedSkill, SkillLoader, SkillLoadError, SkillManifest

#: Returns the bytes behind a source string. Injected so a test never opens a socket and
#: a deployment can swap in an authenticated client without touching this module.
Fetcher = Callable[[str, "InstallPolicy"], Awaitable[bytes]]


class SkillInstallError(ValueError):
    """An install failed. ``code`` names the stage so the caller can react precisely."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class InstallPolicy(FrozenModel):
    """What counts as an acceptable package, and how much it may do while proving it."""

    #: 200 KB is roughly ten times the instruction limit; anything larger is not a skill.
    max_bytes: int = Field(default=200_000, ge=1_024, le=5_000_000)
    #: Empty means "any host", which is only sane with a pinned digest or an allowlist.
    allowed_hosts: tuple[str, ...] = ()
    require_https: bool = True
    #: Local paths and ``file://`` sources are how a developer installs from a checkout;
    #: a deployment that only installs from a registry turns both off.
    allow_local_sources: bool = True
    fetch_timeout_seconds: float = Field(default=30.0, gt=0)
    max_redirects: int = Field(default=5, ge=0, le=20, strict=True)
    smoke_timeout_seconds: float = Field(default=120.0, gt=0)
    #: A package with no smoke command proves nothing. True refuses it.
    require_smoke: bool = True
    #: Refuse to replace a promoted package whose bytes differ.
    allow_upgrade: bool = False


class SmokeResult(FrozenModel):
    """The evidence a candidate produced before promotion."""

    ran: bool
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    output: str = ""
    backend: str = ""


class InstalledSkill(FrozenModel):
    """A promoted skill plus the record of what was proved and what was dropped."""

    manifest: SkillManifest
    instructions: str
    path: str
    source: str
    digest: str
    dropped_tools: tuple[str, ...] = ()
    smoke: SmokeResult

    @property
    def name(self) -> str:
        return self.manifest.name

    def to_loaded(self) -> LoadedSkill:
        """Return the loader view without a second parse of the file on disk."""
        return LoadedSkill(manifest=self.manifest, instructions=self.instructions, source=self.path)


def _validate_remote_source(source: str, policy: InstallPolicy) -> None:
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or (
        policy.require_https and parsed.scheme != "https"
    ):
        raise SkillInstallError("skill source scheme is not allowed", code="source_rejected")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise SkillInstallError("skill source authority is not allowed", code="source_rejected")
    if policy.allowed_hosts and parsed.hostname not in policy.allowed_hosts:
        raise SkillInstallError("skill source host is not allowed", code="source_rejected")


class _PolicyRedirects(urllib.request.HTTPRedirectHandler):
    """Apply the source policy to every hop before urllib opens its destination."""

    def __init__(self, policy: InstallPolicy) -> None:
        self.policy = policy
        self.hops = 0

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        _validate_remote_source(newurl, self.policy)
        if self.hops >= self.policy.max_redirects:
            raise SkillInstallError("skill source exceeded redirect limit", code="source_rejected")
        self.hops += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_with_urllib(source: str, policy: InstallPolicy) -> bytes:
    """Download one package over HTTP(S) with a byte bound and no redirect surprises."""
    parsed = urlparse(source)
    if parsed.scheme in ("", "file"):
        if not policy.allow_local_sources:
            raise SkillInstallError("local skill sources are disabled", code="source_rejected")
        path = Path(parsed.path if parsed.scheme == "file" else source)
        try:
            with path.open("rb") as source_file:
                body = source_file.read(policy.max_bytes + 1)
        except OSError as exc:
            raise SkillInstallError(f"cannot read {path}: {exc}", code="fetch_failed") from exc
        if len(body) > policy.max_bytes:
            raise SkillInstallError("skill package exceeds the size limit", code="too_large")
        return body
    _validate_remote_source(source, policy)
    try:
        opener = urllib.request.build_opener(_PolicyRedirects(policy))
        with opener.open(source, timeout=policy.fetch_timeout_seconds) as response:
            fetched: bytes = response.read(policy.max_bytes + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise SkillInstallError(f"cannot fetch {source}: {exc}", code="fetch_failed") from exc
    if len(fetched) > policy.max_bytes:
        raise SkillInstallError("skill package exceeds the size limit", code="too_large")
    return fetched


def _frontmatter(manifest: SkillManifest) -> str:
    """Emit deterministic YAML by hand: the loader's contract, with no dependency.

    ``yaml.safe_dump`` orders keys by its own rules and quotes differently between
    versions, which would make a promoted package's bytes depend on the installed PyYAML.
    The manifest is five scalars and two lists, so writing it directly is both smaller and
    reproducible -- and the digest recorded at install time has to be reproducible.
    """

    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [
        "---",
        f"name: {quote(manifest.name)}",
        f"version: {quote(manifest.version)}",
        f"description: {quote(manifest.description)}",
        "allowed_tools:" + ("" if manifest.allowed_tools else " []"),
        *(f"  - {quote(tool)}" for tool in manifest.allowed_tools),
        "smoke_command:" + ("" if manifest.smoke_command else " []"),
        *(f"  - {quote(part)}" for part in manifest.smoke_command),
        "---",
    ]
    return "\n".join(lines) + "\n"


class SkillInstaller:
    """Run the five-stage pipeline and promote only what survived it."""

    def __init__(
        self,
        *,
        available_tools: set[str],
        loader: SkillLoader | None = None,
        policy: InstallPolicy | None = None,
        fetcher: Fetcher | None = None,
        backend: BackendFactory | None = None,
        trace: object | None = None,
    ) -> None:
        self._available_tools = available_tools
        self._loader = loader or SkillLoader()
        self._policy = policy or InstallPolicy()
        self._fetcher = fetcher or _default_fetcher
        # A package's declared command is untrusted executable code. The operator
        # must explicitly choose its execution boundary before it can run.
        self._backend_factory = backend
        self._trace = trace

    def _emit(self, event: str, payload: dict[str, object]) -> None:
        append = getattr(self._trace, "append", None)
        if callable(append):
            document = dict(payload)
            source = document.get("source")
            if isinstance(source, str):
                parsed = urlparse(source)
                if parsed.scheme and parsed.netloc:
                    document["source"] = parsed._replace(
                        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""
                    ).geturl()
                document["source_sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            append(event, CredentialMask().transform(document))

    async def install(self, source: str, *, destination: Path) -> InstalledSkill:
        """Install one package from ``source`` into ``destination``."""
        body = await self._fetch(source)
        self._emit("skill.fetched", {"source": source, "bytes": len(body)})

        with tempfile.TemporaryDirectory(prefix="forgeharness-skill-") as scratch:
            staging = Path(scratch)
            entry = staging / "SKILL.md"
            entry.write_bytes(body)
            loaded = self._validate(entry, source)
            trimmed, dropped = self._trim(loaded)
            smoke = await self._smoke(trimmed, staging)
            promoted = self._promote(trimmed, body, destination)
        return InstalledSkill(
            manifest=trimmed.manifest,
            instructions=trimmed.instructions,
            path=str(promoted),
            source=source,
            digest=hashlib.sha256(body).hexdigest(),
            dropped_tools=dropped,
            smoke=smoke,
        )

    async def _fetch(self, source: str) -> bytes:
        try:
            body = await asyncio.wait_for(
                self._fetcher(source, self._policy), timeout=self._policy.fetch_timeout_seconds
            )
        except TimeoutError as exc:
            raise SkillInstallError(f"timed out fetching {source}", code="fetch_failed") from exc
        if len(body) > self._policy.max_bytes:
            raise SkillInstallError("skill package exceeds the size limit", code="too_large")
        return body

    def _validate(self, entry: Path, source: str) -> LoadedSkill:
        try:
            loaded = self._loader.load(entry)
        except SkillLoadError as exc:
            raise SkillInstallError(str(exc), code="invalid_package") from exc
        self._emit(
            "skill.validated",
            {
                "source": source,
                "name": loaded.manifest.name,
                "version": loaded.manifest.version,
                "declared_tools": list(loaded.manifest.allowed_tools),
            },
        )
        return loaded

    def _trim(self, loaded: LoadedSkill) -> tuple[LoadedSkill, tuple[str, ...]]:
        dropped = loaded.unavailable_tools(self._available_tools)
        trimmed = loaded.trimmed(self._available_tools)
        if dropped:
            # Recorded, not silent: a package that lost half its tools may be useless in
            # this deployment, and the operator has to be able to see why.
            self._emit("skill.trimmed", {"name": loaded.manifest.name, "dropped": list(dropped)})
        return trimmed, dropped

    async def _smoke(self, loaded: LoadedSkill, staging: Path) -> SmokeResult:
        command = loaded.manifest.smoke_command
        if not command:
            if self._policy.require_smoke:
                raise SkillInstallError(
                    f"skill {loaded.manifest.name} declares no smoke_command",
                    code="smoke_missing",
                )
            return SmokeResult(ran=False)
        if self._backend_factory is None:
            raise SkillInstallError(
                "a smoke execution backend must be explicitly configured",
                code="smoke_backend_required",
            )
        backend = self._backend_factory(staging)
        try:
            result = await asyncio.wait_for(
                backend.run(command), timeout=self._policy.smoke_timeout_seconds
            )
        except TimeoutError as exc:
            raise SkillInstallError(
                f"smoke test for {loaded.manifest.name} timed out", code="smoke_failed"
            ) from exc
        smoke = SmokeResult(
            ran=True,
            command=command,
            exit_code=result.exit_code,
            output=result.output[-4_000:],
            backend=backend.name,
        )
        self._emit(
            "skill.smoked",
            {
                "name": loaded.manifest.name,
                "backend": smoke.backend,
                "exit_code": smoke.exit_code,
            },
        )
        if result.exit_code != 0:
            raise SkillInstallError(
                f"smoke test for {loaded.manifest.name} exited {result.exit_code}",
                code="smoke_failed",
            )
        return smoke

    def _promote(self, loaded: LoadedSkill, body: bytes, destination: Path) -> Path:
        target = destination / loaded.manifest.name
        manifest = loaded.manifest
        payload = _frontmatter(manifest) + "\n" + loaded.instructions + "\n"
        with _promotion_root(destination) as directory, _promotion_lock(directory, manifest.name):
            exists, current = _installed_manifest(directory, manifest.name)
            if current == payload.encode("utf-8"):
                return target
            if exists and not self._policy.allow_upgrade:
                raise SkillInstallError(
                    f"skill {manifest.name} is already installed with different content",
                    code="already_installed",
                )
            staged = f".promote-{uuid4().hex}"
            backup = f".backup-{manifest.name}-{uuid4().hex}"
            backed_up = False
            os.mkdir(staged, mode=0o700, dir_fd=directory)
            try:
                staged_directory = os.open(
                    staged, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    output_fd = os.open(
                        "SKILL.md",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=staged_directory,
                    )
                    with os.fdopen(output_fd, "wb") as output:
                        output.write(payload.encode("utf-8"))
                        output.flush()
                        os.fsync(output.fileno())
                    os.fsync(staged_directory)
                finally:
                    os.close(staged_directory)
                if exists:
                    os.rename(manifest.name, backup, src_dir_fd=directory, dst_dir_fd=directory)
                    backed_up = True
                try:
                    os.replace(staged, manifest.name, src_dir_fd=directory, dst_dir_fd=directory)
                except BaseException:
                    if backed_up:
                        # The per-skill lock excludes other installer writers. Do
                        # not remove an unexpected entry created outside that lock.
                        if not _entry_exists(directory, manifest.name):
                            os.rename(
                                backup, manifest.name, src_dir_fd=directory, dst_dir_fd=directory
                            )
                    raise
                os.fsync(directory)
                if backed_up:
                    shutil.rmtree(backup, dir_fd=directory)
            finally:
                if _entry_exists(directory, staged):
                    shutil.rmtree(staged, dir_fd=directory)
        self._emit(
            "skill.promoted",
            {
                "name": manifest.name,
                "version": manifest.version,
                "path": str(target),
                "digest": hashlib.sha256(body).hexdigest(),
            },
        )
        return target


@contextmanager
def _promotion_root(destination: Path) -> Iterator[int]:
    """Hold a no-follow POSIX directory chain throughout promotion."""
    absolute = Path(os.path.abspath(destination))
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            try:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except OSError:
                raise SkillInstallError(
                    "skill destination must contain only real directories",
                    code="destination_rejected",
                ) from None
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _promotion_lock(directory: int, name: str) -> Iterator[None]:
    try:
        descriptor = os.open(
            f".{name}.lock",
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
    except OSError:
        raise SkillInstallError(
            "skill install lock is unavailable", code="install_conflict"
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SkillInstallError("skill install lock must be a file", code="install_conflict")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SkillInstallError(
                "skill installation is already running", code="install_conflict"
            ) from None
        yield
    finally:
        os.close(descriptor)


def _entry_exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _installed_manifest(directory: int, name: str) -> tuple[bool, bytes | None]:
    try:
        target = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return False, None
    except OSError:
        raise SkillInstallError(
            "installed skill must be a real directory", code="destination_rejected"
        ) from None
    try:
        try:
            entry = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=target)
        except FileNotFoundError:
            return True, None
        except OSError:
            raise SkillInstallError(
                "installed manifest must be a real file", code="destination_rejected"
            ) from None
        with os.fdopen(entry, "rb") as manifest:
            if not stat.S_ISREG(os.fstat(manifest.fileno()).st_mode):
                raise SkillInstallError(
                    "installed manifest must be a file", code="destination_rejected"
                )
            return True, manifest.read(5_000_001)
    finally:
        os.close(target)


async def _default_fetcher(source: str, policy: InstallPolicy) -> bytes:
    """Run the blocking urllib fetch off the event loop."""
    return await asyncio.to_thread(fetch_with_urllib, source, policy)


__all__ = [
    "Fetcher",
    "InstallPolicy",
    "InstalledSkill",
    "SkillInstallError",
    "SkillInstaller",
    "SmokeResult",
    "fetch_with_urllib",
]

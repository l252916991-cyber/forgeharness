"""Local operations for the single-machine commerce reference application.

The backup boundary is the whole application state directory. SQLite databases are
copied through SQLite's backup API so committed WAL content is included; other
durable files (traces and checkpoints) are copied byte-for-byte. Runtime locks and
SQLite transient files are recreated and therefore excluded.

This module deliberately supports restore only to the original absolute path. SDK
run identities bind state paths and versions, so copying state elsewhere is not a
qualified migration mechanism.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

BACKUP_MANIFEST = "backup-manifest.json"
APPLICATION_BINDING = "application-binding.json"
LOCK_FILE = ".commerce-service.lock"
_TRANSIENT_SUFFIXES = (".sqlite-wal", ".sqlite-shm", ".lock")


class BackupError(ValueError):
    """The requested backup or restore violates the supported local contract."""


class _ApplicationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    application_id: str
    application_version: str


def _application_binding(root: Path) -> _ApplicationBinding:
    marker = root / APPLICATION_BINDING
    if marker.is_symlink():
        raise BackupError("application binding refuses symbolic links")
    if not marker.exists():
        return _ApplicationBinding(application_id="commerce-consultation", application_version="2")
    try:
        return _ApplicationBinding.model_validate_json(marker.read_text())
    except (OSError, ValueError) as exc:
        raise BackupError("application binding is invalid") from exc


def bind_application_state(root: Path, *, application_id: str, application_version: str) -> None:
    """Bind new local state while its caller holds the service lock.

    Existing unmarked durable state is a legacy consultation deployment and is
    never adopted by a different application. A corrupt marker fails closed.
    """
    state = _private_directory(root)
    wanted = _ApplicationBinding(
        application_id=application_id, application_version=application_version
    )
    marker = state / APPLICATION_BINDING
    if marker.exists() or marker.is_symlink():
        if _application_binding(state) != wanted:
            raise BackupError("application state binding differs")
        return
    durable = _durable_files(state)
    if durable and _application_binding(state) != wanted:
        raise BackupError("legacy application state cannot be adopted")
    with marker.open("x") as handle:
        handle.write(wanted.model_dump_json(indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    marker.chmod(0o600)


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    application_id: str = "commerce-consultation"
    application_version: str = "2"
    source_root: str
    created_at: datetime
    files: dict[str, str]
    excluded_transient_suffixes: tuple[str, ...] = _TRANSIENT_SUFFIXES


def _private_directory(path: Path, *, create: bool = False) -> Path:
    resolved = path.resolve()
    if create:
        resolved.mkdir(parents=False, mode=0o700)
    if not resolved.is_dir():
        raise BackupError("state path must be a directory")
    if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise BackupError("state directory must be owner-private (mode 0700)")
    return resolved


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _durable_files(root: Path) -> tuple[Path, ...]:
    files = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError("state backup refuses symbolic links")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.name == LOCK_FILE or relative.name.endswith(_TRANSIENT_SUFFIXES):
            continue
        files.append(relative)
    return tuple(sorted(files))


@contextmanager
def service_lock(root: Path) -> Iterator[None]:
    """Hold the single-host service lock for a running app or an offline operation."""
    state = _private_directory(root)
    handle = (state / LOCK_FILE).open("a")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("commerce service or another state operation is active") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _copy_sqlite(source: Path, destination: Path) -> None:
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db,
        sqlite3.connect(destination) as destination_db,
    ):
        source_db.backup(destination_db)
    destination.chmod(0o600)


def _mkdir_private(path: Path, *, root: Path) -> None:
    """Create each directory below ``root`` with owner-only permissions."""
    relative = path.relative_to(root)
    current = root
    current.chmod(0o700)
    for part in relative.parts:
        current /= part
        current.mkdir(exist_ok=True, mode=0o700)
        current.chmod(0o700)


def _copy_durable(source: Path, destination: Path, *, root: Path) -> None:
    _mkdir_private(destination.parent, root=root)
    if source.suffix == ".sqlite":
        _copy_sqlite(source, destination)
    else:
        shutil.copyfile(source, destination)
        destination.chmod(0o600)


def create_backup(state_root: Path, destination: Path) -> BackupManifest:
    """Create a new, immutable-by-convention backup directory.

    The running application holds the same non-blocking lock, so backup is refused
    while it is serving. ``destination`` must not exist and must sit outside the
    source tree.
    """
    source = _private_directory(state_root)
    target = destination.resolve()
    if target.exists():
        raise BackupError("backup destination already exists")
    if target == source or target.is_relative_to(source):
        raise BackupError("backup destination must be outside application state")
    if not target.parent.is_dir():
        raise BackupError("backup destination parent must exist")
    with service_lock(source):
        target.mkdir(mode=0o700)
        try:
            digests: dict[str, str] = {}
            durable = _durable_files(source)
            binding = _application_binding(source)
            for relative in durable:
                copied = target / relative
                _copy_durable(source / relative, copied, root=target)
                digests[str(relative)] = _digest(copied)
            manifest = BackupManifest(
                application_id=binding.application_id,
                application_version=binding.application_version,
                source_root=str(source),
                created_at=datetime.now(UTC),
                files=digests,
            )
            manifest_path = target / BACKUP_MANIFEST
            with manifest_path.open("x") as file:
                file.write(manifest.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
            manifest_path.chmod(0o600)
            return manifest
        except BaseException:
            shutil.rmtree(target)
            raise


def verify_backup(backup: Path) -> BackupManifest:
    root = _private_directory(backup)
    manifest_path = root / BACKUP_MANIFEST
    try:
        manifest = BackupManifest.model_validate_json(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise BackupError("backup manifest is missing or invalid") from exc
    actual = {str(path) for path in _durable_files(root) if path.name != BACKUP_MANIFEST}
    if actual != set(manifest.files):
        raise BackupError("backup file membership differs from manifest")
    for relative, expected in manifest.files.items():
        if _digest(root / relative) != expected:
            raise BackupError(f"backup file digest changed: {relative}")
    binding = _application_binding(root)
    if (manifest.application_id, manifest.application_version) != (
        binding.application_id,
        binding.application_version,
    ):
        raise BackupError("backup application binding differs from manifest")
    return manifest


def restore_backup(backup: Path, target_root: Path) -> BackupManifest:
    """Restore a verified backup to its original, currently absent absolute path."""
    manifest = verify_backup(backup)
    source = backup.resolve()
    target = target_root.resolve()
    if str(target) != manifest.source_root:
        raise BackupError("restore target must equal the original absolute state path")
    if target.exists():
        raise BackupError("restore target must not already exist")
    if not target.parent.is_dir():
        raise BackupError("restore target parent must exist")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    staging.chmod(0o700)
    try:
        for relative in manifest.files:
            destination = staging / relative
            _mkdir_private(destination.parent, root=staging)
            shutil.copyfile(source / relative, destination)
            destination.chmod(0o600)
        staging.replace(target)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest


def manifest_summary(manifest: BackupManifest) -> dict[str, Any]:
    return {
        "application_id": manifest.application_id,
        "application_version": manifest.application_version,
        "source_root": manifest.source_root,
        "created_at": manifest.created_at.isoformat(),
        "file_count": len(manifest.files),
    }


__all__ = [
    "APPLICATION_BINDING",
    "BACKUP_MANIFEST",
    "BackupError",
    "BackupManifest",
    "bind_application_state",
    "create_backup",
    "manifest_summary",
    "restore_backup",
    "service_lock",
    "verify_backup",
]

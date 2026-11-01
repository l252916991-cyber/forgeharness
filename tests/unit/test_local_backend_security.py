"""Workspace directory swaps never redirect file operations to an outside tree."""

import asyncio
import hashlib
import os
import threading
from pathlib import Path

import pytest

from forgeharness.backends import local
from forgeharness.backends.container import ContainerBackend
from forgeharness.backends.local import MAX_SEARCH_FILE_BYTES, LocalBackend
from forgeharness.backends.protocol import NotRegularFile
from forgeharness.coding.repository import PythonRepositoryMap
from forgeharness.coding.tools import ReadFileInput, ReadFileTool, WriteFileInput, WriteFileTool
from forgeharness.tools.base import ToolContext
from forgeharness.tools.paths import WorkspacePathError


def workspace(tmp_path):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    (root / "nested").mkdir(parents=True)
    outside.mkdir()
    (root / "nested/file.txt").write_text("workspace bytes")
    (outside / "file.txt").write_text("outside bytes")
    return root, outside, LocalBackend(root)


def swap_parent(root: Path, outside: Path) -> None:
    (root / "nested").rename(root / "held-directory")
    (root / "nested").symlink_to(outside, target_is_directory=True)


def test_write_holds_the_approved_parent_when_its_name_is_replaced(tmp_path, monkeypatch):
    root, outside, backend = workspace(tmp_path)
    original = local._atomic_write_at

    def swap_before_replace(directory, name, content, mode):
        swap_parent(root, outside)
        original(directory, name, content, mode)

    monkeypatch.setattr(local, "_atomic_write_at", swap_before_replace)
    written = backend.write_text(
        "nested/file.txt",
        "new workspace bytes",
        expected_digest=hashlib.sha256(b"workspace bytes").hexdigest(),
    )

    assert written.created is False
    assert (outside / "file.txt").read_text() == "outside bytes"
    assert (root / "held-directory/file.txt").read_text() == "new workspace bytes"
    assert sorted(path.name for path in outside.iterdir()) == ["file.txt"]


@pytest.mark.parametrize("operation", ["read", "digest"])
def test_read_and_hash_use_the_held_parent_after_a_directory_swap(
    tmp_path,
    monkeypatch,
    operation,
):
    root, outside, backend = workspace(tmp_path)
    original = backend._file

    def swap_before_open(parent, name):
        swap_parent(root, outside)
        return original(parent, name)

    monkeypatch.setattr(backend, "_file", swap_before_open)
    if operation == "read":
        assert backend.read_text("nested/file.txt").text == "workspace bytes"
    else:
        assert (
            backend.digest_of("nested/file.txt") == hashlib.sha256(b"workspace bytes").hexdigest()
        )


def test_a_parent_substituted_before_open_is_refused_without_reading_outside(tmp_path, monkeypatch):
    root, outside, backend = workspace(tmp_path)
    original = os.open
    swapped = False

    def swap_before_directory_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "nested" and not swapped:
            swapped = True
            swap_parent(root, outside)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(local.os, "open", swap_before_directory_open)
    with pytest.raises(WorkspacePathError):
        backend.read_text("nested/file.txt")
    assert (outside / "file.txt").read_text() == "outside bytes"


async def test_listing_and_search_never_follow_workspace_symlinks(tmp_path):
    root, outside, backend = workspace(tmp_path)
    (root / ".env").write_text("private-search-sentinel")
    (root / "public.txt").symlink_to(root / ".env")
    (root / "outside-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(NotRegularFile):
        backend.read_text("public.txt")
    with pytest.raises(NotRegularFile):
        backend.digest_of("public.txt")
    with pytest.raises(WorkspacePathError):
        backend.write_text("public.txt", "overwrite")
    with pytest.raises(WorkspacePathError):
        await backend.search("outside bytes", path="outside-link")
    result = await backend.search("private-search-sentinel")
    assert result.exit_code == 1 and "private-search-sentinel" not in result.output
    listing = {entry.path for entry in backend.list_files()}
    assert "public.txt" not in listing and "outside-link/file.txt" not in listing


@pytest.mark.parametrize("operation", ["list", "search"])
async def test_walk_skips_a_directory_swapped_to_an_outside_symlink(
    tmp_path,
    monkeypatch,
    operation,
):
    root, outside, backend = workspace(tmp_path)
    original = os.open
    swapped = False

    def swap_before_directory_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "nested" and not swapped:
            swapped = True
            swap_parent(root, outside)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(local.os, "open", swap_before_directory_open)
    if operation == "list":
        assert backend.list_files() == ()
    else:
        result = await backend.search("outside bytes")
        assert result.exit_code == 1 and "outside bytes" not in result.output


async def test_coding_tools_inherit_the_same_no_follow_boundary(tmp_path, monkeypatch):
    root, outside, _ = workspace(tmp_path)
    (root / ".env").write_text("private-tool-sentinel")
    (root / "alias.txt").symlink_to(root / ".env")
    read = await ReadFileTool().execute(
        ReadFileInput(path="alias.txt"), ToolContext(task_id="run", workspace=root)
    )
    assert not read.ok and "private-tool-sentinel" not in read.content
    original = local._atomic_write_at

    def swap_before_replace(directory, name, content, mode):
        swap_parent(root, outside)
        original(directory, name, content, mode)

    monkeypatch.setattr(local, "_atomic_write_at", swap_before_replace)
    written = await WriteFileTool().execute(
        WriteFileInput(
            path="nested/file.txt",
            content="approved workspace update",
            expected_sha256=hashlib.sha256(b"workspace bytes").hexdigest(),
        ),
        ToolContext(task_id="run", workspace=root),
    )
    assert written.ok
    assert (outside / "file.txt").read_text() == "outside bytes"


async def test_search_bounds_reads_and_output_and_reports_skipped_files(tmp_path):
    (tmp_path / "a.txt").write_text("small match\n")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_SEARCH_FILE_BYTES + 1))
    backend = LocalBackend(tmp_path)
    incomplete = await backend.search("missing")
    assert incomplete.exit_code == 1 and incomplete.truncated
    bounded = await backend.search("match", max_output_bytes=7)
    assert bounded.exit_code == 0 and bounded.truncated
    assert len(bounded.output.encode()) <= 7


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "alias"])
async def test_container_search_cannot_read_its_image_outside_the_workspace(tmp_path, path):
    (tmp_path / "alias").symlink_to("/etc", target_is_directory=True)
    runner = pytest.fail
    backend = ContainerBackend(tmp_path, image="unused-image", runner=runner)
    with pytest.raises(WorkspacePathError):
        await backend.search("secret", path=path)


def test_repository_map_refuses_a_parent_swapped_after_listing(tmp_path, monkeypatch):
    root, outside, _ = workspace(tmp_path)
    (root / "nested/module.py").write_text("def workspace_symbol(): pass\n")
    (outside / "module.py").write_text("def outside_private_symbol(): pass\n")
    original = LocalBackend.read_text
    swapped = False

    def swap_before_read(self, path, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            swap_parent(root, outside)
        return original(self, path, **kwargs)

    monkeypatch.setattr(LocalBackend, "read_text", swap_before_read)
    mapped = PythonRepositoryMap().build(root)

    assert len(mapped) == 1
    assert mapped[0].path == "nested/module.py"
    assert mapped[0].parse_error == "file changed during map" and not mapped[0].symbols
    assert "outside_private_symbol" not in str(mapped)


async def test_search_cancellation_stops_scanning_empty_directories(tmp_path, monkeypatch):
    for index in range(100):
        (tmp_path / f"empty-{index}").mkdir()
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = os.scandir

    class Scanner:
        def __init__(self, directory):
            self.inner = original(directory)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()
            stopped.set()

        def __iter__(self):
            entered.set()
            assert release.wait(timeout=5)
            yield from self.inner

    monkeypatch.setattr(local.os, "scandir", Scanner)
    task = asyncio.create_task(LocalBackend(tmp_path).search("missing"))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(stopped.wait, 5)


@pytest.mark.parametrize("operation", ["read", "hash", "search", "write"])
async def test_hardlink_alias_cannot_bypass_the_regular_file_boundary(tmp_path, operation):
    root, outside, backend = workspace(tmp_path)
    secret = outside / "private.txt"
    secret.write_text("hardlink-private-sentinel")
    os.link(secret, root / "public.txt")

    if operation == "search":
        result = await backend.search("hardlink-private-sentinel")
        assert result.exit_code == 1 and "hardlink-private-sentinel" not in result.output
    else:
        with pytest.raises(NotRegularFile, match="multiply linked"):
            if operation == "read":
                backend.read_text("public.txt")
            elif operation == "hash":
                backend.digest_of("public.txt")
            else:
                backend.write_text(
                    "public.txt",
                    "replacement",
                    expected_digest=hashlib.sha256(b"hardlink-private-sentinel").hexdigest(),
                )
    assert secret.read_text() == "hardlink-private-sentinel"

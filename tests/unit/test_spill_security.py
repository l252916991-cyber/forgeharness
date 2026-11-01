"""A repository cannot redirect automatic spill writes outside its workspace."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from forgeharness.context.spill import (
    MAX_BLOB_BYTES,
    BlobStore,
    MessageSpill,
    SpillCorrupt,
    SpillError,
)
from forgeharness.domain.models import Message, MessageRole


@pytest.mark.parametrize("component", [".forgeharness", ".forgeharness/context"])
def test_message_spill_refuses_symlink_components(tmp_path, component):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    destination = workspace / component
    destination.parent.mkdir(exist_ok=True)
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SpillError, match="real directories"):
        MessageSpill(workspace).store((Message(role=MessageRole.USER, content="private"),))
    assert list(outside.iterdir()) == []


def test_parent_replaced_by_symlink_after_binding_is_refused(tmp_path):
    directory = tmp_path / "workspace" / "spill"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    store = BlobStore(directory, prefix="obs")
    directory.rmdir()
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SpillError):
        store.put("private")
    assert list(outside.iterdir()) == []


def test_existing_blob_symlink_never_reads_or_writes_its_target(tmp_path):
    directory = tmp_path / "spill"
    directory.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    store = BlobStore(directory, prefix="obs")
    reference = store.reference_for("private")
    (directory / f"{reference.removeprefix('obs://')}.txt").symlink_to(outside)
    with pytest.raises(SpillCorrupt):
        store.read(reference)
    with pytest.raises(SpillCorrupt):
        store.put("private")
    assert outside.read_text() == "private"


def test_predictable_old_temporary_symlink_is_never_opened(tmp_path):
    directory = tmp_path / "spill"
    directory.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged")
    store = BlobStore(directory, prefix="ctx")
    digest = store.reference_for("new blob").removeprefix("ctx://")
    (directory / f".{digest}.tmp").symlink_to(outside)
    reference = store.put("new blob")
    assert store.read(reference) == "new blob"
    assert outside.read_text() == "unchanged"


def test_existing_corrupt_content_is_not_reused_on_put(tmp_path):
    store = BlobStore(tmp_path, prefix="ctx")
    reference = store.put("original")
    path, _ = store.resolve(reference)
    path.write_text("different")
    with pytest.raises(SpillCorrupt):
        store.put("original")


def test_hard_link_is_not_an_authorized_blob(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    directory = tmp_path / "spill"
    directory.mkdir()
    store = BlobStore(directory, prefix="obs")
    reference = store.reference_for("private")
    os.link(outside, directory / f"{reference.removeprefix('obs://')}.txt")
    with pytest.raises(SpillCorrupt, match="single-link"):
        store.read(reference)


def test_fifo_is_refused_without_waiting_for_a_writer(tmp_path):
    store = BlobStore(tmp_path, prefix="obs")
    reference = store.reference_for("fifo")
    os.mkfifo(tmp_path / f"{reference.removeprefix('obs://')}.txt")
    with pytest.raises(SpillCorrupt, match="regular file"):
        store.read(reference)


def test_blob_read_is_bounded_before_loading_content(tmp_path):
    store = BlobStore(tmp_path, prefix="obs")
    reference = store.reference_for("large")
    with (tmp_path / f"{reference.removeprefix('obs://')}.txt").open("wb") as output:
        output.truncate(MAX_BLOB_BYTES + 1)
    with pytest.raises(SpillCorrupt, match="byte limit"):
        store.read(reference)


def test_failed_promotion_removes_its_unique_temporary_file(tmp_path, monkeypatch):
    store = BlobStore(tmp_path, prefix="ctx")

    def failed(*args, **kwargs):
        raise OSError("injected promotion failure")

    monkeypatch.setattr(os, "replace", failed)
    with pytest.raises(OSError, match="injected"):
        store.put("blob")
    assert list(tmp_path.iterdir()) == []


def test_concurrent_identical_writes_remain_complete_and_verifiable(tmp_path):
    store = BlobStore(tmp_path, prefix="ctx")
    with ThreadPoolExecutor(max_workers=4) as executor:
        references = list(executor.map(store.put, ["shared blob"] * 12))
    assert len(set(references)) == 1
    assert store.read(references[0]) == "shared blob"
    assert len(list(tmp_path.iterdir())) == 1


def test_opened_blob_remains_readable_after_atomic_replacement_unlinks_its_inode(
    tmp_path, monkeypatch
):
    store = BlobStore(tmp_path, prefix="ctx")
    reference = store.put("verified old inode")
    path, _ = store.resolve(reference)
    inode = path.stat().st_ino
    original = os.fstat
    observed_links = []

    def replaced(descriptor):
        metadata = original(descriptor)
        if metadata.st_ino == inode and path.exists():
            replacement = tmp_path / "replacement"
            replacement.write_text("verified old inode")
            os.replace(replacement, path)
        result = original(descriptor)
        observed_links.append(result.st_nlink)
        return result

    monkeypatch.setattr(os, "fstat", replaced)
    assert store.read(reference) == "verified old inode"
    assert 0 in observed_links
    assert store.read(reference) == "verified old inode"

"""Publishing cannot rebuild or substitute bytes after candidate qualification."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from benchmarks.framework_snapshot import snapshot
from benchmarks.release_artifacts import stage


@pytest.fixture
def qualified(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="forgeharness"\nversion="1.2.3"\n')
    candidate = tmp_path / "reports/candidate"
    attempt = candidate / "verification/runs/attempt"
    (attempt / "dist").mkdir(parents=True)
    manifest = snapshot(tmp_path)
    manifest_path = candidate / "source.json"
    manifest_path.write_text(json.dumps(manifest))
    artifacts = {}
    for name in ("forgeharness-1.2.3-py3-none-any.whl", "forgeharness-1.2.3.tar.gz"):
        path = attempt / "dist" / name
        path.write_bytes(name.encode())
        artifacts[name] = {
            "path": str(path.relative_to(tmp_path)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    summary = {
        "qualified": True,
        "source_unchanged": True,
        "distribution_version": "1.2.3",
        "candidate_tree_digest": manifest["tree_digest"],
        "candidate_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "attempt_directory": str(attempt.relative_to(tmp_path)),
        "commands": {"full-gates": {"exit_code": 0}},
        "artifacts": artifacts,
    }
    summary_path = candidate / "verification/summary.json"
    summary_path.write_text(json.dumps(summary))
    return tmp_path, candidate, summary_path, summary


def test_stage_uses_verified_bytes_without_rebuilding(qualified):
    root, candidate, _, summary = qualified
    names = stage(root, candidate, Path("dist"))
    assert set(names) == set(summary["artifacts"])
    for name in names:
        assert (root / "dist" / name).read_bytes() == name.encode()


@pytest.mark.parametrize("marker", ["REJECTED.json", "ABORTED.json"])
@pytest.mark.parametrize("kind", ["file", "broken_symlink"])
def test_audit_rejection_overrides_a_passing_gate_summary(qualified, marker, kind):
    root, candidate, _, summary = qualified
    assert summary["qualified"] is True
    if kind == "file":
        (candidate / marker).write_text('{"finding":"confirmed security defect"}')
    else:
        (candidate / marker).symlink_to(candidate / "missing-audit-record")
    with pytest.raises(ValueError, match="rejected or aborted by audit"):
        stage(root, candidate, Path("dist"))
    assert not (root / "dist").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"qualified": False},
        {"source_unchanged": False},
        {"distribution_version": "9.9.9"},
        {"candidate_tree_digest": "other"},
        {"candidate_manifest_sha256": "other"},
        {"commands": {}},
        {"commands": {"failed": {"exit_code": 1}}},
        {"commands": {"false": {"exit_code": False}}},
        {"attempt_directory": "reports/foreign/verification/runs/attempt"},
    ],
)
def test_rejected_evidence_never_creates_publish_directory(qualified, change):
    root, candidate, path, summary = qualified
    path.write_text(json.dumps(summary | change))
    with pytest.raises(ValueError):
        stage(root, candidate, Path("dist"))
    assert not (root / "dist").exists()


@pytest.mark.parametrize("mutation", ["source", "artifact", "path", "membership"])
def test_drift_and_substitution_are_refused(qualified, mutation):
    root, candidate, path, summary = qualified
    name = next(iter(summary["artifacts"]))
    if mutation == "source":
        (root / "README.md").write_text("changed after qualification")
    elif mutation == "artifact":
        (root / summary["artifacts"][name]["path"]).write_bytes(b"changed")
    elif mutation == "path":
        summary["artifacts"][name]["path"] = "reports/another/" + name
        path.write_text(json.dumps(summary))
    else:
        summary["artifacts"].pop(name)
        path.write_text(json.dumps(summary))
    with pytest.raises(ValueError):
        stage(root, candidate, Path("dist"))
    assert not (root / "dist").exists()


def test_staging_refuses_an_external_destination(qualified):
    root, candidate, _, _ = qualified
    external = root.parent / (root.name + "-external")
    with pytest.raises(ValueError, match="inside this workspace"):
        stage(root, candidate, external)
    assert not external.exists()


def test_injected_staging_symlink_cannot_overwrite_an_external_file(qualified, monkeypatch):
    root, candidate, _, summary = qualified
    outside = root.parent / (root.name + "-outside.txt")
    outside.write_text("unchanged")
    artifact = next(iter(summary["artifacts"]))
    original = os.open

    def injected(path, flags, mode=0o777, *, dir_fd=None):
        if path == artifact and flags & os.O_CREAT:
            os.symlink(outside, path, dir_fd=dir_fd)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", injected)
    with pytest.raises(OSError):
        stage(root, candidate, Path("dist"))
    assert outside.read_text() == "unchanged"
    assert not (root / "dist").exists()
    assert not list(root.glob(".release-*"))


def test_second_artifact_failure_leaves_no_partial_publish_directory(qualified, monkeypatch):
    root, candidate, _, _ = qualified
    original = os.open

    def failed(path, flags, mode=0o777, *, dir_fd=None):
        if str(path).endswith(".tar.gz") and flags & os.O_CREAT:
            raise OSError("injected second artifact failure")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failed)
    with pytest.raises(OSError, match="second artifact"):
        stage(root, candidate, Path("dist"))
    assert not (root / "dist").exists()
    assert not list(root.glob(".release-*"))


def test_staging_parent_swapped_to_external_symlink_is_refused(qualified, monkeypatch):
    root, candidate, _, _ = qualified
    parent = root / "output"
    parent.mkdir()
    outside = root.parent / (root.name + "-outside")
    outside.mkdir()
    original = os.open

    def swapped(path, flags, mode=0o777, *, dir_fd=None):
        if path == "output" and dir_fd is not None:
            parent.rmdir()
            parent.symlink_to(outside, target_is_directory=True)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapped)
    with pytest.raises(OSError):
        stage(root, candidate, Path("output/dist"))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("substitution", ["fifo", "oversized", "hardlink", "symlink"])
def test_artifact_inputs_cannot_block_or_escape_the_read_limit(qualified, substitution):
    from benchmarks.release_artifacts import MAX_ARTIFACT_BYTES

    root, candidate, _, summary = qualified
    name = next(iter(summary["artifacts"]))
    path = root / summary["artifacts"][name]["path"]
    path.unlink()
    if substitution == "fifo":
        os.mkfifo(path)
    elif substitution == "oversized":
        with path.open("wb") as output:
            output.truncate(MAX_ARTIFACT_BYTES + 1)
    else:
        outside = root.parent / (root.name + "-external-artifact")
        outside.write_bytes(name.encode())
        if substitution == "hardlink":
            os.link(outside, path)
        else:
            path.symlink_to(outside)
    with pytest.raises(ValueError):
        stage(root, candidate, Path("dist"))
    assert not (root / "dist").exists()


def test_artifact_parent_changed_after_validation_is_not_followed(qualified, monkeypatch):
    root, candidate, _, summary = qualified
    name = next(iter(summary["artifacts"]))
    path = root / summary["artifacts"][name]["path"]
    saved = path.parent.with_name("saved-dist")
    outside = root.parent / (root.name + "-outside-artifacts")
    outside.mkdir()
    (outside / name).write_bytes(name.encode())
    original = os.open

    def swapped(component, flags, mode=0o777, *, dir_fd=None):
        if component == "dist" and dir_fd is not None:
            path.parent.rename(saved)
            path.parent.symlink_to(outside, target_is_directory=True)
        return original(component, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapped)
    with pytest.raises(ValueError, match="unsafe path"):
        stage(root, candidate, Path("dist"))
    assert not (root / "dist").exists()


@pytest.mark.parametrize("evidence", ["source.json", "verification/summary.json"])
def test_release_evidence_is_bounded_before_json_loading(qualified, evidence):
    from benchmarks.release_artifacts import MAX_EVIDENCE_BYTES

    root, candidate, _, _ = qualified
    with (candidate / evidence).open("wb") as output:
        output.truncate(MAX_EVIDENCE_BYTES + 1)
    with pytest.raises(ValueError, match="bounded"):
        stage(root, candidate, Path("dist"))
    assert not (root / "dist").exists()

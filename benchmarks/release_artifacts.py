"""Stage only the exact artifacts from a qualified, unchanged candidate attempt."""

import argparse
import contextlib
import hashlib
import json
import os
import stat
import tomllib
from pathlib import Path
from uuid import uuid4

from benchmarks.framework_snapshot import snapshot

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


def _read_artifact(root: Path, path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
    """Read a bounded regular artifact without traversing a substituted link."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        relative = path.relative_to(root)
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > max_bytes
            ):
                raise ValueError("release artifact must be a bounded single-link regular file")
            contents = source.read(max_bytes + 1)
            if len(contents) > max_bytes:
                raise ValueError("release artifact exceeds its byte limit")
            return contents
    except OSError:
        raise ValueError("release artifact is unavailable or has an unsafe path") from None
    finally:
        os.close(parent)


def _stage_files(root: Path, destination: Path, contents: dict[str, bytes]) -> None:
    """Promote a complete private staging directory using no-follow descriptors."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in destination.parent.relative_to(root).parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(component, 0o700, dir_fd=parent)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        try:
            os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("release output already exists")
        temporary = ".release-" + uuid4().hex
        os.mkdir(temporary, 0o700, dir_fd=parent)
        directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        promoted = False
        try:
            for name, data in contents.items():
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            os.fsync(directory)
            os.rename(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            promoted = True
            os.fsync(parent)
        finally:
            # Unlinking an injected entry never follows its target. The private
            # directory is only promoted after every verified artifact is ready.
            if not promoted:
                for name in contents:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=directory)
            os.close(directory)
            with contextlib.suppress(FileNotFoundError, OSError):
                os.rmdir(temporary, dir_fd=parent)
    finally:
        os.close(parent)


def stage(root: Path, candidate: Path, output: Path) -> tuple[str, ...]:
    root = root.resolve()
    candidate = (root / candidate).resolve()
    if not candidate.is_relative_to(root / "reports"):
        raise ValueError("release candidate must be inside workspace reports")
    if any(os.path.lexists(candidate / name) for name in ("REJECTED.json", "ABORTED.json")):
        raise ValueError("release candidate was rejected or aborted by audit")
    manifest_path = candidate / "source.json"
    manifest_bytes = _read_artifact(root, manifest_path, max_bytes=MAX_EVIDENCE_BYTES)
    manifest = json.loads(manifest_bytes)
    summary = json.loads(
        _read_artifact(root, candidate / "verification/summary.json", max_bytes=MAX_EVIDENCE_BYTES)
    )
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if (
        summary.get("qualified") is not True
        or summary.get("source_unchanged") is not True
        or summary.get("distribution_version") != version
        or summary.get("candidate_tree_digest") != manifest["tree_digest"]
        or snapshot(root)["tree_digest"] != manifest["tree_digest"]
        or summary.get("candidate_manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
        or not summary.get("commands")
        or any(
            type(check.get("exit_code")) is not int or check["exit_code"] != 0
            for check in summary["commands"].values()
        )
    ):
        raise ValueError("release requires a qualified unchanged candidate and all gates")
    expected = (f"forgeharness-{version}-py3-none-any.whl", f"forgeharness-{version}.tar.gz")
    if set(summary.get("artifacts", {})) != set(expected):
        raise ValueError("release requires exactly the qualified wheel and sdist")
    attempt = (root / summary["attempt_directory"]).resolve()
    if not attempt.is_relative_to(candidate / "verification/runs"):
        raise ValueError("release attempt is outside this candidate")
    contents = {}
    for name in expected:
        artifact = summary["artifacts"][name]
        path = root / artifact["path"]
        if path.is_symlink() or path.resolve() != attempt / "dist" / name:
            raise ValueError("release artifact does not belong to the qualified attempt")
        data = _read_artifact(root, path)
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError("qualified artifact bytes changed")
        contents[name] = data
    destination = (root / output).resolve()
    if destination == root or not destination.is_relative_to(root):
        raise ValueError("release output must be inside this workspace")
    _stage_files(root, destination, contents)
    return expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    staged = stage(Path(__file__).resolve().parents[1], args.candidate_dir, args.output)
    print(json.dumps({"staged": staged}))


if __name__ == "__main__":
    main()

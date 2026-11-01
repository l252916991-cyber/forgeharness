"""Content-addressed candidate identity for archive exports without Git metadata."""

import hashlib
import json
from pathlib import Path

# Root-level files that belong to the reviewed identity. `.gitignore` is included
# because the packaging tool ships it in the sdist: every sdist member must be a
# frozen file, so leaving it out would either fail the allowlist or ship an
# unreviewed input. A listed file that is absent is simply not part of the digest;
# removing it still changes the identity.
ROOT_FILES = (
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    ".python-version",
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
)


def snapshot(root: Path) -> dict[str, object]:
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for directory in (
        "src",
        "tests",
        "docs",
        "benchmarks",
        "evals",
        "examples",
        ".github",
        "deploy",
        "data",
    ):
        paths.extend(path for path in (root / directory).rglob("*") if path.is_file())
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    digest = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "source_kind": "archive checkout equivalent; no Git metadata",
        "tree_digest": digest,
        "files": hashes,
    }


def main() -> None:
    manifest = snapshot(Path.cwd())
    destination = Path("reports/framework-candidate-source.json")
    if destination.exists():
        previous = json.loads(destination.read_text())
        if manifest != previous:
            raise SystemExit(
                "candidate source changed; classify changes and refreeze before verification"
            )
        print(f"candidate verified: {manifest['tree_digest']}")
    else:
        destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        print(f"candidate frozen: {manifest['tree_digest']}")


if __name__ == "__main__":
    main()

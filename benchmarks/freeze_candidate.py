#!/usr/bin/env python3
"""Freeze the exact tracked and unignored ForgeHarness candidate without committing it."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from forgeharness.review import candidate_paths, snapshot_file, snapshot_tree_hash


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    files = tuple(snapshot_file(root, relative) for relative in candidate_paths(root))
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "revision": revision,
        "source_tree_sha256": snapshot_tree_hash(files),
        "files": [item.model_dump(mode="json") for item in files],
    }
    output = root / "reports" / "candidate-manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(output)
    print(
        f"candidate frozen: revision={revision} files={len(files)} "
        f"sha256={payload['source_tree_sha256']}"
    )


if __name__ == "__main__":
    main()

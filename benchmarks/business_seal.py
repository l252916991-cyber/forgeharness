"""One-shot sealed-set governance. Default development evaluation never reads it."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Targets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    sealed_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scoring_version: str = Field(min_length=1)
    frozen_at: datetime
    thresholds: dict[str, float] = Field(min_length=1)
    abort_conditions: tuple[str, ...] = Field(min_length=1)
    model: str = Field(min_length=1)
    source_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1)


def freeze_targets(path: Path, targets: Targets) -> None:
    """Never replace a prior declaration with revised thresholds."""
    if targets.frozen_at.tzinfo is None or targets.frozen_at > datetime.now(UTC):
        raise ValueError("target freeze needs a past timezone-aware timestamp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as file:
        file.write(targets.model_dump_json(indent=2) + "\n")
        file.flush()
        os.fsync(file.fileno())


def open_sealed(path: Path, *, targets_path: Path, marker_dir: Path) -> Any:
    """Mark consumed before decoding or execution; even a failed run uses the set.

    The host owns the seal and markers. Deleting markers or replacing the candidate
    is an audit violation, not a supported retry mechanism.
    """
    started = datetime.now(UTC)
    targets = Targets.model_validate_json(targets_path.read_text())
    if targets.frozen_at.tzinfo is None or targets.frozen_at >= started:
        raise ValueError("quality targets must precede opening")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != targets.sealed_sha256:
        raise ValueError("sealed set differs from the frozen target")
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{digest}.opened.json"
    with marker.open("x") as file:
        json.dump(
            {
                "state": "opened",
                "opened_at": started.isoformat(),
                "sealed_sha256": digest,
                "targets_sha256": hashlib.sha256(targets_path.read_bytes()).hexdigest(),
            },
            file,
        )
        file.flush()
        os.fsync(file.fileno())
    return json.loads(raw)

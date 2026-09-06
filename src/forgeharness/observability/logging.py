"""Structured JSON logging for operators, complementing the hash-chained trace.

The trace answers "what did this run do"; these logs answer "is the service
healthy right now" at INFO/WARNING/ERROR granularity. No third-party dependency:
a stdlib `logging` formatter emits one JSON object per line so `jq` and any
log shipper can consume it without custom parsing.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_LOGGER_NAME = "forgeharness"
_RESERVED: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys() | {"message", "asctime"}
)


class JsonFormatter(logging.Formatter):
    """Format each record as one JSON object with UTC timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, level: int = logging.INFO, stream: Any = sys.stderr) -> None:
    """Attach exactly one JSON handler to the `forgeharness` logger tree."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the `forgeharness` namespace."""
    if not name.startswith(f"{_LOGGER_NAME}."):
        name = f"{_LOGGER_NAME}.{name}"
    return logging.getLogger(name)

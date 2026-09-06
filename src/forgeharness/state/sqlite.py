"""Shared bounded SQLite WAL connection initialization."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def connect_wal(path: Path, *, foreign_keys: bool = False) -> sqlite3.Connection:
    """Open SQLite in WAL mode, retrying only the concurrent-init lock race."""
    deadline = time.monotonic() + 30
    while True:
        connection = sqlite3.connect(path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            if foreign_keys:
                connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.OperationalError as exc:
            connection.close()
            if "database is locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)

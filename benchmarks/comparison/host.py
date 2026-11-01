"""Explicit shared host contract, not a claimed native framework capability.

Only synthetic fixtures are supported. SQLite keeps business effects separate from
agent checkpoints so the oracle can observe a commit even when its worker dies.
This is an experiment host, not a public authentication or production service.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

CONTENT = "Synthetic price is 1299 cents."
PAYLOAD = "Synthetic support request"
CRASH_EXIT = 73


def model_input_json(value: Any) -> Any:
    """Preserve non-JSON set types explicitly; never silently stringify objects."""

    def encode(item: Any) -> Any:
        if isinstance(item, (set, frozenset)) and all(isinstance(x, str) for x in item):
            return {"__python_type__": type(item).__name__, "items": sorted(item)}
        raise TypeError("unsupported model input type: " + type(item).__name__)

    return json.loads(json.dumps(value, default=encode))


class Denied(PermissionError):
    pass


class Conflict(ValueError):
    pass


class Ledger:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "host.sqlite"
        with self.connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY,body TEXT NOT NULL,"
                "state TEXT NOT NULL,result TEXT,source_version INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS effects (id TEXT PRIMARY KEY,payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY,"
                "kind TEXT NOT NULL,payload TEXT NOT NULL);"
            )
            for key, value in {"allowed": True, "published": True, "version": 1}.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def event(self, kind: str, payload: Any) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(kind,payload) VALUES (?,?)", (kind, json.dumps(payload)))

    def require_current(self, version: int | None = None) -> None:
        if not self.get("allowed") or not self.get("published"):
            raise Denied("current host access or publication is unavailable")
        if version is not None and version != self.get("version"):
            raise Denied("source version changed")

    def model_input(self, messages: Any) -> None:
        # This records the real input to the deterministic model, not token costs.
        if self.count("model_input") >= 4:
            raise Conflict("fixture model request budget exhausted")
        value = model_input_json(messages)
        value["evidence_format"] = "structured-model-input-v2"
        self.event("model_input", value)

    def count(self, kind: str) -> int:
        with self.connect() as db:
            return int(
                db.execute("SELECT count(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]
            )

    def request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def settle(self, request_id: str, state: str, result: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET state=?,result=? WHERE id=?", (state, result, request_id)
            )
        self.event("host_state", {"request": request_id, "state": state})
        return {"state": state, "result": result}

    def effect(self, request_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM effects WHERE id=?", (request_id,)).fetchone()
        return str(row[0]) if row else None

    def effects(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT count(*) FROM effects").fetchone()[0])

    def read(self, payload: str) -> str:
        self.require_current()
        self.event("tool_read", {"payload": payload, "version": self.get("version")})
        return CONTENT

    async def write(self, request_id: str, payload: str) -> str:
        self.require_current()
        row = self.request(request_id)
        if row is None or row["state"] != "approved":
            raise Denied("host has no independently approved exact request")
        if json.loads(row["body"])["payload"] != payload:
            raise Denied("tool arguments differ from the approved payload")
        self.event("tool_write_started", {"request": request_id})
        if self.get("fault") == "cancel_before":
            self.set("cancel_requested", True)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO effects VALUES (?,?)", (request_id, payload))
            stored = db.execute("SELECT payload FROM effects WHERE id=?", (request_id,)).fetchone()
            if stored[0] != payload:
                raise Conflict("business key reused with changed contents")
        self.event("business_commit", {"request": request_id, "payload": payload})
        if self.get("fault") == "lost_receipt":
            # A genuine process exit after a durable business commit; no cleanup.
            os._exit(CRASH_EXIT)
        if self.get("fault") == "cancel_after":
            self.set("cancel_requested", True)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        return "ticket:" + request_id


class Host:
    def __init__(self, ledger: Ledger, engine: Any) -> None:
        self.ledger, self.engine = ledger, engine

    @staticmethod
    def body(operation: str, payload: str) -> str:
        if (
            operation not in {"read", "write"}
            or not isinstance(payload, str)
            or not 0 < len(payload) <= 2000
        ):
            raise ValueError("unsupported operation or fixture payload")
        return json.dumps({"operation": operation, "payload": payload}, sort_keys=True)

    async def execute(
        self,
        *,
        action: str,
        request_id: str = "request-1",
        operation: str = "write",
        payload: str = PAYLOAD,
        approver: str = "reviewer",
    ) -> dict[str, Any]:
        if action not in {"submit", "approve"}:
            raise ValueError("unsupported host action")
        if request_id != "request-1":
            raise ValueError("this fixture uses one declared request identity per directory")
        body = self.body(operation, payload)
        db = self.ledger
        # A reproducible await boundary. The injected host withdrawal happens
        # before calling any model adapter, and is attributed to this host.
        await asyncio.sleep(0)
        if db.get("fault") == "revoke_during_wait":
            db.set("allowed", False)
            db.event("host_revocation", {"boundary": "after_wait_before_model"})
        db.require_current()
        with db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO requests VALUES (?,?,?,NULL,?)",
                (request_id, body, "new", db.get("version")),
            )
        row = db.request(request_id)
        assert row is not None
        if row["body"] != body:
            raise Conflict("stable request identity has different contents")
        db.require_current(row["source_version"])
        if row["state"] == "succeeded":
            return {"state": "succeeded", "result": row["result"], "replayed": True}
        if row["state"] == "cancelled":
            return {"state": "cancelled", "result": None}
        if db.effect(request_id) is not None:
            db.event("host_reconciliation", {"request": request_id})
            return db.settle(request_id, "succeeded", "ticket:" + request_id)
        if action == "submit" and row["state"] == "pending":
            return {"state": "pending", "result": None}
        if action == "approve":
            if approver != "reviewer" or row["state"] != "pending":
                raise Denied("independent reviewer and pending request required")
            db.settle(request_id, "approved")
        elif row["state"] != "new":
            raise Conflict("unfinished execution needs explicit reconciliation")
        try:
            result = await self.engine.run(
                db,
                request_id=request_id,
                operation=operation,
                payload=payload,
                resume=action == "approve",
            )
        except asyncio.CancelledError:
            if db.effect(request_id) is not None:
                db.event("host_reconciliation", {"request": request_id, "after_cancel": True})
                return db.settle(request_id, "succeeded", "ticket:" + request_id)
            return db.settle(request_id, "cancelled")
        db.require_current(row["source_version"])
        return db.settle(request_id, result["state"], result.get("result"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

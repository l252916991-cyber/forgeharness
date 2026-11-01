"""Independent process for synchronized real-PG admission/claim races."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from forgeharness.cloud.models import CloudConflict, RunDefinition, TenantContext
from forgeharness.cloud.store import PostgresRunStore


def worker_dsn() -> str:
    parsed = urlsplit(os.environ["FH_TEST_PG_WORKER_DSN"])
    query = dict(parse_qsl(parsed.query))
    query["user"] = os.environ["FH_TEST_PG_WORKER_ROLE"]
    database = os.environ["FH_TEST_PG_WORKER_DATABASE"]
    query["database"] = database
    return urlunsplit(
        (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], "/" + database, urlencode(query), "")
    )


async def main(payload: dict) -> None:
    store = await PostgresRunStore.connect(worker_dsn())
    try:
        context = TenantContext.model_validate(payload["context"])
        if payload["operation"] == "submit":
            run = await store.submit(
                context,
                payload["request_key"],
                RunDefinition.model_validate(payload["definition"]),
            )
            result = {"status": "accepted", "run_id": run.run_id, "pid": os.getpid()}
        elif payload["operation"] == "claim":
            try:
                lease = await store.claim(
                    context, payload["run_id"], worker_id=payload["worker_id"]
                )
                result = {"status": "claimed", "epoch": lease.epoch, "pid": os.getpid()}
            except CloudConflict:
                result = {"status": "conflict", "pid": os.getpid()}
        else:
            raise ValueError("unsupported test operation")
        print(json.dumps(result), flush=True)
    finally:
        await store.close()


if __name__ == "__main__":
    print("READY", flush=True)
    # Parent releases both independent interpreters after they have started.
    request = json.loads(sys.stdin.readline())
    asyncio.run(main(request))

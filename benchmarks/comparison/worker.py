"""One action in one process. All request material is synthetic JSON on stdin."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from benchmarks.comparison.adapters import ENGINES
from benchmarks.comparison.host import Conflict, Denied, Host, Ledger


async def execute(engine: str, root: Path, body: dict) -> dict:
    ledger = Ledger(root)
    try:
        return await Host(ledger, ENGINES[engine]()).execute(**body)
    except Denied:
        return {"state": "denied", "result": None}
    except Conflict:
        return {"state": "conflict", "result": None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=sorted(ENGINES), required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    import sys

    body = json.load(sys.stdin)
    result = asyncio.run(execute(args.engine, args.root, body))
    print("RESULT:" + json.dumps({**result, "worker_pid": os.getpid()}))


if __name__ == "__main__":
    main()

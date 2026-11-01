"""Process death at real parallel persistence boundaries; synthetic receipts only."""

import asyncio
import json
import os
import sys
from pathlib import Path

from examples.parallel_demo import ReceiptModel, execute
from forgeharness.sdk.durable import DurableRunner
from forgeharness.state.orchestration import OrchestrationStore


def main():
    point = os.environ.get("PARALLEL_CRASH_AT", "")
    original_save = DurableRunner._save
    original_ack = OrchestrationStore.acknowledge
    original_model = ReceiptModel.decide

    def save(self, event, **kwargs):
        if point == "before:" + event:
            os._exit(91)
        original_save(self, event, **kwargs)
        if point == event:
            os._exit(91)

    def acknowledge(self, task, revision):
        if point == "before:outbox-ack":
            os._exit(91)
        original_ack(self, task, revision)

    async def model(self, request):
        result = await original_model(self, request)
        if point == "response:alpha":
            os._exit(91)
        return result

    DurableRunner._save = save
    OrchestrationStore.acknowledge = acknowledge
    ReceiptModel.decide = model
    root, action = Path(sys.argv[1]), sys.argv[2]
    node = sys.argv[3] if len(sys.argv) > 3 else "specialist.beta"
    print(json.dumps(asyncio.run(execute(root, action, node))))


if __name__ == "__main__":
    main()

"""Optional LlamaIndex value adapter over the same ForgeHarness retriever."""

from __future__ import annotations

import asyncio
from pathlib import Path

from llama_index.core.schema import NodeWithScore, TextNode

from forgeharness.knowledge.application import build_knowledge_application


async def main() -> None:
    application = build_knowledge_application(Path(".forgeharness-framework-compare"))
    try:
        report = await application.knowledge.search("How does approval work?")
        nodes = [
            NodeWithScore(
                node=TextNode(
                    text=hit.chunk.content,
                    metadata={
                        "source": hit.chunk.source,
                        "chunk_id": hit.chunk.id,
                    },
                ),
                score=hit.score,
            )
            for hit in report.final
        ]
        print(nodes)
    finally:
        await application.close()


if __name__ == "__main__":
    asyncio.run(main())

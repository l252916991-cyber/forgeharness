"""Optional LangChain adapter over the exact ForgeHarness retrieval pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda

from forgeharness.knowledge.application import build_knowledge_application


async def main() -> None:
    application = build_knowledge_application(Path(".forgeharness-framework-compare"))

    async def retrieve(query: str) -> list[Document]:
        report = await application.knowledge.search(query)
        return [
            Document(
                page_content=hit.chunk.content,
                metadata={
                    "source": hit.chunk.source,
                    "chunk_id": hit.chunk.id,
                    "score": hit.score,
                },
            )
            for hit in report.final
        ]

    chain = RunnableLambda(retrieve)
    try:
        print(await chain.ainvoke("How does approval work?"))
    finally:
        await application.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Read-only reviewer for grounded answers and source-linked citations."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.knowledge.models import Citation, RetrievalReport
from forgeharness.knowledge.protocols import ChatModel, StructuredChatModel


class ReviewVerdict(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


class ReviewDecision(FrozenModel):
    verdict: ReviewVerdict
    reason: str = Field(min_length=1, max_length=1_000)


class CitationReviewer:
    """Validate citations first, then optionally ask a model for semantic review.

    The reviewer receives only immutable answer/evidence values. It has no Tool
    Registry, shell, file-write, or memory-write capability.
    """

    def __init__(self, model: ChatModel) -> None:
        self._model = model

    async def review(
        self,
        *,
        question: str,
        answer: str,
        citations: tuple[Citation, ...],
        report: RetrievalReport,
    ) -> ReviewDecision:
        deterministic = _validate_citations(citations, report)
        if deterministic is not None:
            return deterministic
        if not citations and "不知道" not in answer and "do not know" not in answer.lower():
            return ReviewDecision(
                verdict=ReviewVerdict.REJECT,
                reason="answer makes a claim without any citation",
            )
        if not isinstance(self._model, StructuredChatModel):
            return ReviewDecision(
                verdict=ReviewVerdict.ACCEPT,
                reason="citation bindings passed deterministic validation",
            )
        payload = await self._model.complete_json(
            system=(
                "Review whether the answer is supported by the evidence. You are read-only. "
                "Return accept, revise, or reject and a concise reason."
            ),
            user=json.dumps(
                {
                    "question": question,
                    "answer": answer,
                    "evidence": [citation.model_dump(mode="json") for citation in citations],
                },
                ensure_ascii=False,
            ),
            schema={
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": [item.value for item in ReviewVerdict],
                    },
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "required": ["verdict", "reason"],
                "additionalProperties": False,
            },
        )
        try:
            return ReviewDecision.model_validate(json.loads(response_json(payload)))
        except ValueError:
            return ReviewDecision(
                verdict=ReviewVerdict.REJECT,
                reason="reviewer returned invalid structured output",
            )


def _validate_citations(
    citations: tuple[Citation, ...], report: RetrievalReport
) -> ReviewDecision | None:
    chunks = {hit.chunk.id: hit.chunk for hit in report.final}
    for citation in citations:
        chunk = chunks.get(citation.chunk_id)
        if chunk is None:
            return ReviewDecision(
                verdict=ReviewVerdict.REJECT,
                reason=f"citation {citation.chunk_id} is absent from retrieval evidence",
            )
        if citation.source != chunk.source or citation.quote not in chunk.content:
            return ReviewDecision(
                verdict=ReviewVerdict.REJECT,
                reason=f"citation {citation.chunk_id} does not match the stored chunk",
            )
        if (
            citation.page != chunk.page
            or citation.start_line != chunk.start_line
            or citation.end_line != chunk.end_line
        ):
            return ReviewDecision(
                verdict=ReviewVerdict.REJECT,
                reason=f"citation {citation.chunk_id} has a false source location",
            )
    return None


def response_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("reviewer returned no JSON object")
    return text[start : end + 1]

"""Read-only legal DAG: parallel deterministic specialists + one Critic call.

This is a framework prototype over a LexVault-shaped ``CaseBundle``, not the live
LexVault runtime. It proves that an application orchestrator can run deterministic
parallel nodes and then use ``RunKernel.call_model`` for the synthesis step while
trace, budget, checkpoints and verification remain Harness-owned.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Sequence

from forgeharness.applications.legal.adapter import CasePage
from forgeharness.sdk import (
    ApplicationSpec,
    AuthorizationRequest,
    Capability,
    FinalAction,
    Message,
    MessageRole,
    Model,
    PolicyDecision,
    PolicyDecisionType,
    RiskLevel,
    RunKernel,
    RunResult,
    RunStatus,
    VerificationRequest,
    VerificationResult,
    Verifier,
    compose_application,
)

from .adapter import BundleCaseAdapter
from .bundle import CaseBundle, text_digest

_FACT_TERMS = ("资金", "金额", "支付", "合同", "约定", "转账")
_EVIDENCE_TERMS = ("证据", "附件", "凭证", "清单", "发票", "流水")
_CONTRADICTION_TERMS = ("矛盾", "不一致", "否认", "相反", "未收到")
_PAGE_MARKER = re.compile(r"\[page:(\d+)\]")
# A citation quote must directly follow its page marker: [page:N]「exact span」.
_PAGE_QUOTE = re.compile(r"\[page:(\d+)\]「([^」]*)」")
_INSUFFICIENT_TERMS = ("材料不足", "无法判断", "不足以")

_CRITIC_SYSTEM = (
    "You are a legal review critic. Use only the supplied specialist findings. "
    "Cite every factual sentence as [page:N] followed by the exact quote in corner "
    "brackets, for example [page:2]「exact span copied from the finding's quote」, "
    "where N is a page number from the findings and the quoted text is copied "
    "verbatim from that finding. Never paraphrase inside the quote brackets. "
    "If the findings cannot support an answer, say 材料不足 and do not invent facts. "
    "Do not give a legal conclusion; a lawyer must review the result."
)


class ReadOnlyDagPolicy:
    """The DAG app exposes no tools; if one is added, only reads are allowed."""

    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        if request.spec.risk is RiskLevel.READ:
            return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="read-only DAG")
        return PolicyDecision(
            type=PolicyDecisionType.DENY,
            reason="the read-only LexVault DAG exposes no mutating tool",
        )

    def can_approve(self, *, requester: object, owner: object, call: object) -> bool:
        del requester, owner, call
        return False


class BundleCitationVerifier:
    """Verify bundle citations: page identity, exact quoted spans and page digests.

    Citation forms:

    * ``[page:N]`` — page identity only (default mode; kept for compatibility).
    * ``[page:N]「exact quote」`` — the quote must be a verbatim substring of that
      page's raw text; an empty or altered quote fails.
    * ``strict_quotes=True`` — every ``[page:N]`` must carry a quote block.

    Every cited page's ``text_sha256`` is recomputed at verification time, so a
    bundle object whose text was mutated after construction fails closed. This
    still does not assess semantic support or legal correctness.
    """

    def __init__(self, bundle: CaseBundle, *, strict_quotes: bool = False) -> None:
        self._pages = {page.page_no: page for page in bundle.pages}
        self._strict_quotes = strict_quotes

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        content = request.final.content.strip()
        if not content:
            return VerificationResult(passed=False, reason="answer is empty")
        markers = [int(value) for value in _PAGE_MARKER.findall(content)]
        if not markers:
            if any(term in content for term in _INSUFFICIENT_TERMS):
                return VerificationResult(
                    passed=True,
                    reason="declared insufficient evidence; semantic support not assessed",
                )
            return VerificationResult(
                passed=False,
                reason="a factual answer must cite at least one [page:N]",
            )
        unknown = sorted({value for value in markers if value not in self._pages})
        if unknown:
            return VerificationResult(
                passed=False,
                reason="citation points to a page outside the bundle: "
                + ", ".join(str(value) for value in unknown),
            )
        quoted = _PAGE_QUOTE.findall(content)
        if self._strict_quotes and len(quoted) < len(markers):
            return VerificationResult(
                passed=False,
                reason=(
                    "strict citation mode requires an exact 「quote」 directly after every [page:N]"
                ),
            )
        evidence: list[str] = []
        for raw_page, quote in quoted:
            page = self._pages[int(raw_page)]
            if not quote.strip():
                return VerificationResult(
                    passed=False,
                    reason=f"empty quote for page {page.page_no}",
                )
            if quote not in page.text:
                return VerificationResult(
                    passed=False,
                    reason=(
                        f"quote is not an exact span of page {page.page_no}; "
                        "paraphrase or stale text is not a citation"
                    ),
                )
            evidence.append(f"page:{page.page_no}:{page.text_sha256[:12]}")
        for number in sorted(set(markers)):
            page = self._pages[number]
            if text_digest(page.text) != page.text_sha256:
                return VerificationResult(
                    passed=False,
                    reason=f"page {number} text no longer matches its digest",
                )
            if not any(item.startswith(f"page:{number}:") for item in evidence):
                evidence.append(f"page:{number}")
        return VerificationResult(
            passed=True,
            reason=(
                "bundle citation valid (page identity, exact quote where present, "
                "digest match); semantic support not assessed"
            ),
            evidence=tuple(evidence),
        )


def _task_terms(task: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(term for term in re.split(r"\W+", task.lower()) if len(term) >= 2))


def _find_quote(page: CasePage, terms: Sequence[str]) -> str | None:
    for term in terms:
        position = page.content.find(term)
        if position >= 0:
            start = position
            end = min(len(page.content), start + 180)
            return page.content[start:end]
    return None


class LexVaultDagOrchestrator:
    """Three deterministic specialists in parallel, then one Critic model call."""

    def __init__(self, adapter: BundleCaseAdapter, *, case_id: str) -> None:
        self._adapter = adapter
        self._case_id = case_id

    async def run(self, *, kernel: RunKernel, task: str) -> RunResult:
        view = self._adapter.case(kernel.context, self._case_id)
        pages = [
            self._adapter.page(kernel.context, self._case_id, locator)
            for locator in view.page_locators
        ]
        task_terms = _task_terms(task)
        facts, evidence, contradiction = await asyncio.gather(
            self._specialist(
                kernel=kernel,
                node="facts",
                pages=pages,
                terms=(*task_terms, *_FACT_TERMS),
            ),
            self._specialist(
                kernel=kernel,
                node="evidence",
                pages=pages,
                terms=(*task_terms, *_EVIDENCE_TERMS),
            ),
            self._specialist(
                kernel=kernel,
                node="contradiction",
                pages=pages,
                terms=(*task_terms, *_CONTRADICTION_TERMS),
            ),
        )
        findings = [facts, evidence, contradiction]
        result = await kernel.call_model(
            messages=(
                Message(role=MessageRole.SYSTEM, content=_CRITIC_SYSTEM),
                Message(
                    role=MessageRole.USER,
                    content=json.dumps(
                        {"task": task, "specialists": findings},
                        ensure_ascii=False,
                    ),
                ),
            ),
            phase="critic",
        )
        action = result.action
        if not isinstance(action, FinalAction):
            return await kernel.finish(
                status=RunStatus.FAILED,
                error="critic returned a non-final action",
            )
        kernel.messages.append(Message(role=MessageRole.ASSISTANT, content=action.content))
        return await kernel.finish(final=action)

    async def _specialist(
        self,
        *,
        kernel: RunKernel,
        node: str,
        pages: list[CasePage],
        terms: Sequence[str],
    ) -> dict[str, object]:
        # Yield once so the three specialists are genuinely scheduled together.
        await asyncio.sleep(0)
        findings: list[dict[str, object]] = []
        for page in pages:
            quote = _find_quote(page, terms)
            if quote is not None:
                findings.append({"page": self._citation_no(page), "quote": quote})
        kernel.emit(
            "dag.node.completed",
            {"node": node, "findings": len(findings)},
        )
        return {
            "node": node,
            "findings": findings,
            "note": "" if findings else "no lexical match",
        }

    def _citation_no(self, page: CasePage) -> int:
        bundle_page = self._adapter.bundle.page_by_locator(page.locator)
        if bundle_page is None:
            raise ValueError(f"page is not part of the case bundle: {page.locator}")
        return bundle_page.page_no


def lexvault_dag_application(
    *,
    model_factory: Callable[[], Model],
    verifier: Verifier,
    application_id: str = "lexvault-dag",
    version: str = "1",
    policy_version: str = "1",
) -> ApplicationSpec:
    """Compose the read-only LexVault DAG application."""

    return compose_application(
        id=application_id,
        version=version,
        policy_version=policy_version,
        model_factory=model_factory,
        capabilities=(
            Capability(
                "lexvault-dag",
                instructions=(
                    "Use only page findings; cite [page:N]; a lawyer must review the result.",
                ),
            ),
        ),
        policy=ReadOnlyDagPolicy(),
        verifier=verifier,
    )


__all__ = [
    "BundleCitationVerifier",
    "LexVaultDagOrchestrator",
    "ReadOnlyDagPolicy",
    "lexvault_dag_application",
]

"""Keyless development contracts; this report never qualifies real business quality."""

import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from benchmarks.framework_snapshot import snapshot
from examples.commerce_demo import build as commerce_build
from examples.commerce_demo import identity as commerce_identity
from forgeharness.applications.legal.adapter import SyntheticCaseAdapter
from forgeharness.applications.legal.pilot import (
    LegalAnswer,
    case_qa_application,
    publish_case_pages,
)
from forgeharness.sdk import (
    Actor,
    Citation,
    FinalAction,
    Harness,
    ModelRequest,
    ModelResult,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
    ToolAction,
    ToolCall,
)

ROOT = Path(__file__).resolve().parents[1]


def load_dev(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text())
    if value.get("schema_version") != 1 or value.get("split") != "dev":
        raise ValueError("only declared development scenarios are accepted")
    cases = value["cases"]
    if not isinstance(cases, list) or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("development case identities must be unique")
    if (
        sum(c["domain"] == "legal" for c in cases) != 40
        or sum(c["domain"] == "commerce" for c in cases) != 80
    ):
        raise ValueError("expected 40 legal and 80 commerce contract scenarios")
    if any(not c.get("family") or c["domain"] not in {"legal", "commerce"} for c in cases):
        raise ValueError("case family and supported domain required")
    return cases


def legal_identity(case: dict[str, Any], role: str) -> RunContext:
    count = 6 if case["case_id"] == "case-alpha" else 2
    return RunContext(
        application_id="legal-case-qa",
        application_version="1",
        policy_version="1",
        run_id="evaluation",
        session_id=case["id"],
        correlation_id="synthetic",
        actor=Actor(id=role, roles=(role,)),
        scope=ResourceScope(
            tenant_id=case["tenant"],
            resources=(
                f"case:{case['case_id']}",
                *(f"case:{case['case_id']}/page:{p}" for p in range(1, count + 1)),
            ),
        ),
    )


class LegalFixtureModel:
    def __init__(self, case: dict[str, Any]):
        self.case = case

    async def decide(self, request: ModelRequest) -> ModelResult:
        observations = [m for m in request.messages if m.role.value == "tool"]
        if not observations:
            if self.case["kind"] in {"search", "missing"}:
                name = "search_case"
                arguments = {
                    "case_id": self.case["case_id"],
                    "query": "synthetic" if self.case["kind"] == "search" else "notavailablexyz",
                }
            else:
                name = "read_case_page"
                arguments = {
                    "case_id": self.case["case_id"],
                    "locator": f"page:{self.case['page']}",
                }
            return ModelResult(
                action=ToolAction(call=ToolCall(id="read", name=name, arguments=arguments))
            )
        value = json.loads(observations[-1].content or "{}")
        if "content" in value:
            quote = value["content"]
            citation = Citation(
                resource_id=value["resource_id"],
                version=value["version"],
                digest=value["digest"],
                start=0,
                end=len(quote),
                quote=quote,
            )
        elif value.get("evidence"):
            e = value["evidence"][0]
            citation = Citation(
                **{
                    key: e[key]
                    for key in ("resource_id", "version", "digest", "start", "end", "quote")
                }
            )
        else:
            return ModelResult(
                action=FinalAction(
                    content=LegalAnswer(
                        answer="Insufficient evidence.", insufficient_evidence=True
                    ).model_dump_json()
                )
            )
        return ModelResult(
            action=FinalAction(
                content=LegalAnswer(answer=citation.quote, citations=(citation,)).model_dump_json()
            )
        )


async def legal_case(case: dict[str, Any], root: Path) -> dict[str, Any]:
    store = RevisionStore(root / "records.sqlite", policy=RoleRecordPolicy())
    adapter = SyntheticCaseAdapter(ROOT / "data/synthetic/legal")
    owner = legal_identity(case, "editor")
    publish_case_pages(
        adapter,
        store,
        reader=owner,
        reviewer=legal_identity(case, "reviewer"),
        publisher=legal_identity(case, "publisher"),
        case_id=case["case_id"],
    )
    resource = f"case:{case['case_id']}/page:{case['page']}"
    if case["kind"] == "withdrawn":
        store.withdraw(legal_identity(case, "publisher"), resource, expected_version=1)
    elif case["kind"] == "cross_scope":
        owner = owner.model_copy(
            update={"scope": ResourceScope(tenant_id=case["tenant"], resources=())}
        )
    harness = Harness(
        case_qa_application(adapter, store, model_factory=lambda: LegalFixtureModel(case)),
        state_dir=root / "runs",
    )
    result = await harness.run(context=owner, task=f"Synthetic {case['kind']} check for {resource}")
    negative = case["kind"] in {"withdrawn", "cross_scope"}
    # A declared refusal may be verified, but it may not contain factual citations.
    answer = (
        LegalAnswer.model_validate_json(result.final_output)
        if result.status is RunStatus.SUCCEEDED
        else None
    )
    if negative or case["kind"] == "missing":
        passed = answer is None or (answer.insufficient_evidence and not answer.citations)
    else:
        passed = bool(answer and answer.citations and not answer.insufficient_evidence)
    exact = None
    if answer and answer.citations:
        exact = all(
            store.published(owner, c.resource_id).content[c.start : c.end] == c.quote
            for c in answer.citations
        )
        passed = passed and exact
    return {
        "passed": passed,
        "status": result.status,
        "citation_exact": exact,
        "semantic_correctness": None,
        "clarification": None,
        "handoff": False,
        "duplicate_effects": 0,
        "tool_calls": result.usage.tool_calls,
        "steps": result.usage.steps,
        "baseline": "synthetic adapter only; LexVault not measured",
        "baseline_exact_span_match": exact,
        "input_tokens": None,
        "output_tokens": None,
    }


async def commerce_case(case: dict[str, Any], root: Path) -> dict[str, Any]:
    store, _, service = commerce_build(root)
    owner = commerce_identity("customer", case["id"], case["tenant"])
    service.create(owner)
    params: dict[str, Any] = {"request_id": case["id"], "query": case.get("query", "price")}
    if case.get("sku"):
        params["skus"] = [case["sku"]]
    if case["kind"] == "faq":
        params["faq_ids"] = [case["faq_id"]]
    if case["kind"] == "handoff":
        params["handoff"] = True
    text = json.dumps(params)
    turn = await service.submit(owner, request_id=case["id"], text=text)
    if case["kind"] == "follow_up":
        turn = await service.submit(
            owner,
            request_id=case["id"] + "-follow",
            text=json.dumps(
                {"request_id": case["id"] + "-follow", "query": "stock", "follow_up": True}
            ),
        )
    handoff = turn.status is RunStatus.AWAITING_APPROVAL
    if handoff:
        approver = commerce_identity("supervisor", case["id"], case["tenant"])
        grant = service.approve(owner, turn.id, approver=approver)
        turn = await service.resume(owner, turn.id, grant=grant, approver=approver)
        before = len(store.list_tickets(case["tenant"]))
        repeated = await service.submit(owner, request_id=case["id"], text=text)
        duplicates = len(store.list_tickets(case["tenant"])) - before
        passed = repeated.output == turn.output and before == 1 and duplicates == 0
    else:
        duplicates = 0
        answer = json.loads(turn.output or "{}")
        if case["kind"] == "missing":
            passed = answer.get("status") == "needs_human" and not answer.get("products")
        else:
            passed = bool(answer.get("products"))
            for product in answer.get("products", []):
                live = store.product(case["tenant"], product["sku"])
                passed = (
                    passed
                    and all(product[k] == live[k] for k in ("price_cents", "stock", "version"))
                    and bool(product["as_of"])
                )
    passed = passed and turn.status is RunStatus.SUCCEEDED
    result = service.inspect(owner, turn.id)
    return {
        "passed": bool(passed),
        "status": turn.status,
        "citation_exact": None,
        "semantic_correctness": None,
        "clarification": None,
        "handoff": handoff,
        "duplicate_effects": duplicates,
        "tool_calls": result.usage.tool_calls,
        "steps": result.usage.steps,
        "input_tokens": None,
        "output_tokens": None,
    }


async def evaluate(manifest: Path, output: Path) -> dict[str, Any]:
    cases = load_dev(manifest)
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="forge-business-eval-") as directory:
        for case in cases:
            root = Path(directory) / case["id"]
            root.mkdir(mode=0o700)
            start = time.perf_counter()
            try:
                result = await (
                    legal_case(case, root)
                    if case["domain"] == "legal"
                    else commerce_case(case, root)
                )
            except Exception as exc:
                result = {"passed": False, "error_type": type(exc).__name__}
            outcomes.append({**case, **result, "latency_ms": (time.perf_counter() - start) * 1000})
    times = sorted(r["latency_ms"] for r in outcomes)
    report = {
        "schema_version": 1,
        "mode": "keyless-synthetic-contracts",
        "measurement_valid": False,
        "business_qualified": False,
        "independent_business_questions": None,
        "human_review": None,
        "model": "deterministic fixtures",
        "samples_per_case": 1,
        "case_count": len(cases),
        "source_identity": snapshot(ROOT)["tree_digest"],
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "passed": all(r["passed"] for r in outcomes),
        "p50_ms": statistics.median(times),
        "p95_ms": times[int(len(times) * 0.95) - 1],
        "latency_scope": "includes synthetic store setup; not model/service latency",
        "legal_cases": sum(c["domain"] == "legal" for c in cases),
        "commerce_cases": sum(c["domain"] == "commerce" for c in cases),
        "families": {
            domain: len({c["family"] for c in cases if c["domain"] == domain})
            for domain in ("legal", "commerce")
        },
        "outcomes": outcomes,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/business-development.json")
    args = parser.parse_args()
    report = asyncio.run(evaluate(ROOT / "evals/business/development.json", args.output))
    print(json.dumps({k: v for k, v in report.items() if k != "outcomes"}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

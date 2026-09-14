# Implementation status

This file is the source of truth for the enhanced candidate. **The full user-approved enhancement plan is not yet complete.** The earlier `e3a8a38...` audit covered only the original Harness release and must not authorize the new feature set. The current audit records remaining implementation and evidence gaps explicitly; a passing unit suite is not a final release decision.

## Implemented and tested

| Area | Evidence |
| --- | --- |
| Original Agent Loop, planning, tools, policy, approval, checkpoint, context, Skills, MCP, Sub-agent, memory and hash trace | Existing unit/integration tests under `tests/` |
| Harness-owned completion verification gate before `succeeded`; the coding vertical requires a successful write and passing tests | `runtime/verification.py`, runtime gate tests in `test_runtime.py`, `test_verification.py`, `test_coding_agent.py` retry scenario, ADR 006 |
| OMLX chat/vision, embedding and reranker adapters; loopback proxy isolation; structured intent output | `tests/unit/test_omlx_knowledge.py`, `test_omlx_qualification.py`, `reports/omlx-qualification.json` |
| Markdown/TXT/code/JSON/PDF/image parsing and bounded atomic upload storage | `tests/unit/test_knowledge_parsers_storage.py` |
| FTS5 + vector, RRF, reranking fallback, stage timings, no-evidence refusal and source-bound citations | `tests/unit/test_knowledge_indexes_service.py`, `test_knowledge_reviewer_queue.py` |
| SQLite sessions and asyncpg/PostgreSQL protocol implementation | `knowledge/storage.py`, `docs/migrations/001_sessions.sql` |
| Intent workflows, session isolation, reviewed semantic memory in a separate vector space | `test_knowledge_conversation_api.py` |
| Explicit attachment identity, image/file ContentPart, bounded source-only excerpts | `test_knowledge_runs_attachments.py`; at most the first five chunks across selected attachments |
| Durable RAG responses and real retrieval/model hash-chained traces | `knowledge/runs.py`, `test_knowledge_runs_attachments.py`; ordinary chat/memory-command runs are not covered yet |
| Memory revocation/soft deletion with stale-vector suppression and retryable cleanup | `SemanticMemoryService.revoke`, `test_memory_revocation_suppresses_stale_vectors_when_cleanup_fails` |
| Inline and Redis/ARQ job producers, retry-safe processor and worker entry point | `knowledge/queue.py`, `worker.py`, queue tests |
| FastAPI endpoints, lightweight UI, actual dependency readiness and metrics | API tests and `/docs` |
| Browser file/image upload, job polling, attachment, citations, local Host/Origin guards and CSP | API boundary test and local Edge/Playwright review |
| Workspace-root-bound API coding route with exact-action approval | `coding/api.py`, workspace-binding and approval tests; no multi-process approval guarantee |
| Read-only Reviewer Agent citation validation and enablement experiment | `knowledge/reviewer.py`, reviewer boundary tests, `reports/reviewer-experiment.json` |
| 60-case keyless RAG gate | `evals/rag_cases.json`, `reports/rag-eval.json` |
| 5,000-chunk/10-concurrency local retrieval benchmark | `reports/retrieval-benchmark.json` |
| Live PostgreSQL/Redis/Qdrant/Nginx process-failover drill | `benchmarks/platform_verification.py`, `reports/platform-verification.json` |
| Exact uncommitted candidate snapshot and runtime dependency audit | `reports/candidate-manifest.json`, `reports/dependency-audit-runtime.json` |
| Revision-bound measured framework-comparison benchmark (native vs LangChain retrieval) | `reports/framework-comparison.json`, `forge framework-compare` |

## Remaining full-plan gates

- The `AgentRuntime` now assembles every model request under `RunBudget.max_context_tokens` and compactly folds older turns into a deterministic, trace-linked summary (ADR 005). Compaction is structural, not semantic: it does not judge what mattered, and progressive retrieval and tool-subset retrieval are not implemented. The RAG/chat paths do not share this assembler or the `ContextCompiler`; this must not be described as whole-platform context management.
- The completion verification gate (ADR 006) is installed by default only in the coding vertical, where it requires a successful write and passing tests. Knowledge and general-chat paths are not gated, and no task-specific verifiers (citation, file-existence) exist yet. The gate checks the evidence a task type produces; it is not a general correctness proof.
- The real OMLX suite uses controlled echo/intent/label-image cases, not twenty annotated realistic code screenshots/architecture diagrams. Raw strict-JSON rate, cold/warm TTFT, token throughput, and process memory are not all measured.
- RAG/Reviewer quality results use deterministic models and distinctive fixture tokens. Citation precision currently checks source membership, not semantic claim support. Real-model domain-quality and independent framework comparisons remain outstanding.
- PostgreSQL currently holds sessions/messages only; document/job/idempotency/memory/run metadata remains SQLite on shared local disk. In-flight live worker-kill and outbox recovery are not verified.
- The 200-question personal study bank exists as structured answer outlines; full timed 60–120-second explanations and the learner's three mock-interview results remain unfinished.
- A clean-source verification report covers offline quality checks only. Live reports must be rerun/bound to the final frozen source before final completion.

## Validated decisions

- The full OMLX suite passed 20 tool, 20 intent, 20 vision, 10 embedding, and 30 reranker cases using the three existing local models. No new model was downloaded.
- The live platform drill verified cross-process PostgreSQL sessions and Qdrant search, 30/30 Redis/ARQ jobs after a worker-stop interval, content deduplication across idempotency keys, and 20/20 requests after one API process stopped.
- Reviewer quality was unchanged on the deterministic 60-case set and latency remained within the limit. Because improvement was 0 rather than the required 5 percentage points, Reviewer correctly remains default-off.
- The runtime/platform/development dependency audit found no known vulnerability. The all-extras audit found one no-fix 2026 NLTK advisory inherited only by the optional LlamaIndex comparison; ForgeHarness does not invoke the affected model-artifact APIs.
- LangChain and LlamaIndex adapters are comparison examples only; neither owns ForgeHarness control flow.

## Explicit residual limits

- Path confinement and approval are defense in depth, not an OS sandbox.
- API approval capability is process-local. Checkpoints survive restart, but an old grant cannot be reconstructed.
- Two API processes demonstrate local process failover only. They share the laptop, filesystem, Docker runtime, and OMLX failure domains and therefore are not production high availability.
- The deterministic RAG dataset is a regression/control set, not a representative enterprise corpus.
- The benchmark measures local FTS5 + in-memory vectors, not Qdrant network performance.
- Image chunks are model-derived observations linked to the source image, not OCR ground truth.
- Memory deletion is an audit-preserving tombstone plus index removal, not secure erasure of the original audit record.
- Host/Origin guards protect the local browser boundary, but there is no identity/tenant authorization. Do not expose this profile to a network or public tunnel.
- The worker drill verifies queued work during an outage, not a SIGKILL in the middle of an external write. Automatic lease/outbox recovery is not claimed.
- No SWE-bench result, production SLO, multi-agent superiority, cloud HA, or cost claim is authorized.

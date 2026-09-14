# ForgeHarness

> Enhanced candidate, not a completed release: see [`docs/STATUS.md`](docs/STATUS.md) for implemented features and remaining full-plan gates. The old 91-test audit applies only to the original Harness.

ForgeHarness is an evaluation-first Agent Harness plus a local R&D knowledge agent. It exposes the engineering decisions that higher-level agent frameworks often hide: loop transitions, tool validation, permissions, context selection, checkpoints, hybrid retrieval, reviewed memory, delegation scope, and trace integrity.

The main workflow takes a repository issue, lets a model inspect the codebase, pauses every file mutation for an exact-action approval, runs a fixed test command, inspects the Git diff, and returns test-backed evidence.

## What is implemented

- Direct ReAct-style runtime and a Plan-Execute variant over a provider-neutral model protocol.
- Hard step, tool-call, and token budgets with explicit success, failure, exhaustion, and approval-suspension states.
- Pydantic/JSON Schema tool validation, risk policy, timeouts, bounded output, sanitized subprocess environments, and workspace path confinement.
- Exact, expiring, one-shot approvals plus optimistic SQLite checkpoints.
- Token-budgeted context selection, per-request window assembly with deterministic history compaction, observation compression, and an AST-based Python repository map that never imports repository code.
- A coding agent with list/search/read/optimistic-write/test/diff tools and an end-to-end fixture-repair test.
- Versioned local Skills, stateless HTTP MCP tools, and scoped sub-agents whose usage is charged to the parent.
- Review-gated long-term memory and redacted, fsynced, SHA-256 hash-chained JSONL traces.
- Typer CLI, local FastAPI control surface, and a manifest-driven keyless control evaluation.
- OMLX chat/vision, 2,560-dimensional embedding, and reranker adapters with provider-enforced structured output.
- Markdown/text/code/JSON/PDF/image ingestion, FTS5 + vector retrieval, reciprocal-rank fusion, reranking, source-bound citations, and evidence refusal.
- Durable sessions, intent routing, review-gated semantic memory, a read-only citation Reviewer Agent, idempotent jobs, Inline or Redis/ARQ queues, and real readiness probes.
- Optional asyncpg/PostgreSQL sessions, Qdrant vector spaces, Docker Compose dependencies, two-host-process Nginx balancing, LangChain/LangGraph comparison adapters, and structured JSON request logging.

## Architecture

```text
Web / CLI / FastAPI
        |
Session -> IntentRouter
        +-- knowledge_query -> FTS5 + Vector -> RRF -> Reranker -> citations
        +-- coding_task -> workspace-bound CodingAgent
        +-- memory_command -> candidate -> human review -> memory vectors
        +-- general_chat -> OMLX chat
                                |
AgentRuntime -> ToolRegistry -> Policy -> Dispatcher -> native / MCP / sub-agent
             -> ApprovalLedger / Checkpoint / HashChainedTrace
```

Harness controls remain outside model prompts. Model output, repository text, tool arguments, and MCP responses are treated as untrusted input.

## Quick start

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are required. The `search_code` tool shells out to [`ripgrep`](https://github.com/BurntSushi/ripgrep) (`rg`), which must be on `PATH`.

```bash
uv sync --extra dev --extra platform --extra benchmark
make check
uv run forge demo --text "hello"
uv run forge eval-control
uv run forge eval-rag
uv run forge eval-reviewer
uv run forge bench-retrieval
```

### Framework comparison

ForgeHarness includes both a **native Agent runtime** and a **LangChain/LangGraph implementation** to demonstrate framework proficiency and architectural trade-offs. See [`docs/LANGCHAIN_COMPARISON.md`](docs/LANGCHAIN_COMPARISON.md) for detailed comparison.

```bash
uv sync --extra frameworks
uv run forge langchain-rag "What is ForgeHarness?"
uv run forge langchain-agent "Add tests to example.py"
uv run forge framework-compare  # Full benchmark
```

The control evaluation is keyless and writes [`reports/control-eval.json`](reports/control-eval.json). The fixed 60-case RAG and Reviewer gates write [`reports/rag-eval.json`](reports/rag-eval.json) and [`reports/reviewer-experiment.json`](reports/reviewer-experiment.json); the 5,000-chunk benchmark writes [`reports/retrieval-benchmark.json`](reports/retrieval-benchmark.json).

## Use the local OMLX models

```bash
omlx start
export FORGE_ENABLE_OMLX=true
uv run forge qualify-omlx --quick
uv run forge serve --data-dir .forgeharness --port 8001
```

Defaults are `Qwen3.5-9B-4bit`, `Qwen3-Embedding-4B-4bit-DWQ`, and `bge-reranker-v2-m3-mlx` at `http://127.0.0.1:8000/v1`. Loopback HTTP ignores desktop proxy settings. No model is downloaded by ForgeHarness.

Use `uv run forge qualify-omlx` for the full 100-case capability gate (20 tool, 20 intent, 20 vision, 10 embedding, and 30 reranker cases). The checked-in full report passed with the existing models; the quick form remains only a smoke test.

## Run the coding agent

Use a disposable branch or worktree. The API key can be provided without putting it in shell history:

```bash
export FORGE_API_KEY="..."
uv run forge repair /path/to/git/repo \
  "Describe the defect and acceptance criteria" \
  --model MODEL_NAME \
  --base-url https://provider.example/v1 \
  --test-command "python -m pytest -q"
```

Reads and the configured test command are allowed by the coding policy. Each `write_file` request prints its exact path, content, and expected SHA-256 before approval. `--yes` is available for isolated disposable environments, but interactive approval is the safer default.

Run artifacts are stored under the target repository's `.forgeharness/` directory:

```bash
uv run forge inspect-run TASK_ID --data-dir /path/to/git/repo/.forgeharness
```

## Local API

```bash
uv run forge serve --data-dir .forgeharness
curl http://127.0.0.1:8001/health
curl -X POST http://127.0.0.1:8001/runs/keyless-demo \
  -H 'content-type: application/json' \
  -d '{"text":"controlled"}'
```

Open `http://127.0.0.1:8001/` for the lightweight UI or `/docs` for every endpoint. The UI supports file/image uploads, indexing status, media attachment, and citation-bearing answers. Port 8000 belongs to OMLX; the API defaults to 8001. Coding sessions are disabled unless `FORGE_CODING_WORKSPACE_ROOT` is set. A requested workspace must be relative to that root, must resolve inside it, and must contain `.git`; writes still suspend on exact-action approval.

The local profile rejects non-loopback Host names and cross-origin browser mutations. It has no multi-user authentication and must not be exposed publicly. Approved memories can be revoked with `POST /v1/memories/{id}/review` (`approve: false`) or soft-deleted with `DELETE /v1/memories/{id}`; both remove retrieval indexes, while retaining the local audit record. This is not secure erasure.

For an internal single-tenant deployment, set `FORGE_SERVICE_API_KEY` to a random value of at least 16 characters. `/v1/*` and `/runs/*` then require `X-API-Key` or `Authorization: Bearer ...`; health, metrics, and the UI remain available for probes and operator access. This is an API key boundary, not multi-tenant authorization.

## Platform profile

```bash
docker compose -f deploy/docker-compose.yml up -d
. deploy/run-platform.sh
```

Run the two displayed API commands and the ARQ worker in separate host terminals. Host processes can reach localhost-only OMLX; Nginx listens at `http://127.0.0.1:8080`. This is a local failover demonstration, not a claim of regional high availability.

With OMLX and the four Compose services healthy, `uv run python benchmarks/platform_verification.py` runs the reproducible cross-process session/vector/queue/recovery/failover drill. `uv run python benchmarks/freeze_candidate.py` freezes the exact source snapshot, and `uv run forge review` checks all release evidence and unresolved findings.

## Evidence and scope

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module, state, and trust boundaries.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — metrics, datasets, and reporting rules.
- [`docs/STATUS.md`](docs/STATUS.md) — implemented/planned boundary and known limitations.
- [`docs/REVIEW.md`](docs/REVIEW.md) — mandatory final audit protocol.
- [`docs/REFERENCES.md`](docs/REFERENCES.md) — specifications, baselines, and attribution.

This candidate does not claim SWE-bench performance, Reviewer superiority, OS-level sandboxing, automatic Git worktree creation, Qdrant/PostgreSQL production scale, or crash-safe approval resumption after an API restart. The checked-in reports support only their named, revision-bound workloads.

## License

MIT. External specifications and research baselines retain their own licenses; no upstream agent implementation is copied into the runtime.

# ForgeHarness

ForgeHarness is an evaluation-first Agent Harness with a software-engineering vertical agent. It exposes the engineering decisions that higher-level agent frameworks often hide: loop transitions, tool validation, permissions, context selection, checkpoints, memory review, delegation scope, and trace integrity.

The main workflow takes a repository issue, lets a model inspect the codebase, pauses every file mutation for an exact-action approval, runs a fixed test command, inspects the Git diff, and returns test-backed evidence.

## What is implemented

- Direct ReAct-style runtime and a Plan-Execute variant over a provider-neutral model protocol.
- Hard step, tool-call, and token budgets with explicit success, failure, exhaustion, and approval-suspension states.
- Pydantic/JSON Schema tool validation, risk policy, timeouts, bounded output, sanitized subprocess environments, and workspace path confinement.
- Exact, expiring, one-shot approvals plus optimistic SQLite checkpoints.
- Token-budgeted context selection, observation compression, and an AST-based Python repository map that never imports repository code.
- A coding agent with list/search/read/optimistic-write/test/diff tools and an end-to-end fixture-repair test.
- Versioned local Skills, stateless HTTP MCP tools, and scoped sub-agents whose usage is charged to the parent.
- Review-gated long-term memory and redacted, fsynced, SHA-256 hash-chained JSONL traces.
- Typer CLI, local FastAPI control surface, and a manifest-driven keyless control evaluation.

## Architecture

```text
CLI / FastAPI
      |
CodingAgent ---- ContextCompiler / Skills
      |
AgentRuntime ---- Model adapter
      |
      +---- ToolRegistry -> Policy -> Dispatcher -> native / MCP / sub-agent
      +---- ApprovalLedger
      +---- SQLiteCheckpointStore
      +---- HashChainedJSONLTrace
```

Harness controls remain outside model prompts. Model output, repository text, tool arguments, and MCP responses are treated as untrusted input.

## Quick start

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
uv sync --all-extras
make check
uv run forge demo --text "hello"
uv run forge eval-control
```

The control evaluation is keyless and writes [`reports/control-eval.json`](reports/control-eval.json). It covers normal completion, unknown-tool recovery, tool-budget exhaustion, model failure, approval suspension, trace tamper detection, and memory review gating.

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
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/runs/keyless-demo \
  -H 'content-type: application/json' \
  -d '{"text":"controlled"}'
```

The API intentionally exposes only a keyless demo and read-only run/trace inspection in this release. Credentialed coding runs stay in the interactive CLI approval boundary.

## Evidence and scope

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module, state, and trust boundaries.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — metrics, datasets, and reporting rules.
- [`docs/STATUS.md`](docs/STATUS.md) — implemented/planned boundary and known limitations.
- [`docs/REVIEW.md`](docs/REVIEW.md) — mandatory final audit protocol.
- [`docs/REFERENCES.md`](docs/REFERENCES.md) — specifications, baselines, and attribution.

This release does not claim SWE-bench performance, multi-agent superiority, OS-level sandboxing, automatic Git worktree creation, or crash-safe approval resumption across processes. Those claims require additional implementation or measured experiments.

## License

MIT. External specifications and research baselines retain their own licenses; no upstream agent implementation is copied into the runtime.

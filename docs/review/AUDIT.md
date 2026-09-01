# Final audit report

## Decision

Passed for candidate revision `e3a8a38f5fdb4becdf7ff61fb349ce721be3e169` on 2026-09-02. No blocker or high-severity finding remains open. Medium residual risks are explicit product limits rather than hidden claims.

## Verification evidence

| Check | Result |
| --- | --- |
| `make check` | Passed: 91 tests, strict mypy, ruff format/lint, 90.55% branch-aware coverage |
| `forge eval-control` | Passed: 7/7 cases; report revision and manifest hash match the candidate |
| `forge demo --text reviewed` | Passed: one tool call, final success, six trace events |
| `uv lock --check` | Passed against the configured official PyPI index |
| `pip-audit --path .venv/lib/python3.12/site-packages` | No known vulnerabilities after upgrading pytest; the local unpublished `forgeharness` package is the only skipped distribution |
| Direct dependency license metadata | MIT or BSD-3-Clause for all declared runtime and primary development dependencies |

## Review by dimension

### Architecture

The runtime owns loop transitions, budgets, policy decisions, approvals, checkpoints, and trace events. CLI, FastAPI, model, SQLite, subprocess, and MCP concerns stay behind composition or adapter boundaries. The direct-runtime ADR matches the code.

### Correctness and resilience

Success, failure, exhaustion, policy denial, unknown tools, timeout, approval suspension/resume, stale checkpoints, output compression, and model/protocol errors have deterministic tests. Review fixes now lease remaining parent budget to nested agents and kill the full subprocess group on cancellation.

### Trust and execution boundaries

Model decisions and tool arguments are schema-validated. Repository content is supplied as context/tool data, not as Harness instructions. Filesystem tools reject absolute paths and symlink escapes; writes require a matching prior SHA for existing files and exact external approval. Commands are fixed argv vectors with a credential-filtered environment. MCP shares native registry/policy/dispatcher controls and now has pagination/resource guards.

This is defense in depth, not an OS sandbox. Concurrent hostile filesystem mutation, arbitrary sensitive source text in traces, and durable cross-process approvals remain outside the release guarantee.

### State and observability

SQLite checkpoints use revision compare-and-swap. Approval grants are task/action-bound, expiring, and one-shot. Long-term memories require review before retrieval. JSONL traces recursively redact known credential shapes, fsync each append, and verify sequence and SHA-256 chain integrity.

### Tests and evaluation

The keyless control manifest is immutable by hash and separates Harness-control correctness from real-model quality. The integration suite repairs a real temporary Git repository through read, approval, write, test, diff, and final evidence. No real-model, cost, SWE-bench, or multi-agent-improvement result is claimed.

### Documentation and resume claims

README, architecture, status, evaluation, and Chinese resume guidance agree on implemented behavior. Quantitative control claims point to the revision-bound report. Unsupported claims are listed explicitly in `docs/STATUS.md` and `docs/RESUME_CN.md`.

## Findings summary

- Three high findings were fixed with regression tests: parent budget leasing, generic/MCP resource bounds, and descendant process termination.
- One medium dependency finding was fixed by upgrading pytest and regenerating `uv.lock`.
- Three medium limitations are accepted and documented: no OS sandbox, bounded credential-pattern redaction rather than content classification, and process-local approval grants.
- One low scope item is accepted: credentialed benchmark evidence is deferred.

The machine-readable record is [`FINDINGS.json`](FINDINGS.json).

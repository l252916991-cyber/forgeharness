# Implementation status

This file is the source of truth for the implemented/planned boundary of the current audit candidate.

## Implemented and tested

| Area | Evidence |
| --- | --- |
| ReAct runtime, Plan-Execute, terminal states, token/step/tool budgets | `tests/unit/test_runtime.py`, `tests/unit/test_plan_execute.py` |
| Native tool validation, policy, timeout, cancellation, output bounds | `tests/unit/test_registry_dispatcher.py`, `tests/unit/test_policy.py`, `tests/unit/test_coding_tools.py` |
| Exact approvals and optimistic SQLite checkpoints | `tests/unit/test_approval.py`, `tests/unit/test_checkpoint.py` |
| Context selection, compression, safe Python AST repository map | `tests/unit/test_context.py`, `tests/unit/test_paths_repository.py` |
| Issue-to-approved-write-to-tests-to-diff coding workflow | `tests/integration/test_coding_agent.py` |
| Skills, current stateless HTTP MCP subset, scoped sub-agent accounting | `tests/unit/test_skills.py`, `tests/unit/test_mcp.py`, `tests/unit/test_subagent.py` |
| Reviewed memory and tamper-evident redacted trace | `tests/unit/test_memory_trace.py` |
| CLI, local FastAPI inspection API, seven-case control evaluation | `tests/unit/test_cli_review.py`, `tests/unit/test_api.py`, `reports/control-eval.json` |

## Implemented with explicit limits

- `write_file` is atomic and SHA-bound for existing files, but the release does not automatically create a Git worktree or container.
- Sub-agents have reduced tool scope and parent-owned accounting, but no benchmark currently claims that delegation improves results.
- MCP supports the current stateless HTTP JSON-RPC flow and JSON responses. Streaming `text/event-stream` responses and legacy session negotiation are rejected.
- SQLite checkpoints persist run snapshots, but approval grants are process-local. A stopped CLI process cannot later reconstruct and consume its old approval grant.
- Trace redaction covers credential-shaped keys and values; it does not promise removal of arbitrary sensitive source code returned by tools.
- The test command is passed as a fixed argv vector without a shell. It still runs with the invoking user's OS permissions inside the selected repository.

## Planned, not claimed

- OS-level sandboxing, resource quotas, automatic worktree lifecycle, and patch-file export.
- Durable multi-process approval service and event streaming.
- Dedicated reviewer-agent experiment and single-agent versus reviewer-agent measurements.
- Pinned SWE-bench subset and real-model result/cost reports.
- Web trace explorer, PostgreSQL backend, A2A integration, and non-Python repository maps.

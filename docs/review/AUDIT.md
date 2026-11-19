# Enhanced-candidate audit — 2026-09-04

## Decision: hold, not complete

The offline engineering candidate passes, but the full user-approved enhancement
plan does not yet pass final review. Open high findings FH-015, FH-016, and FH-017
must be resolved; interview preparation also has unfinished scope (FH-018).
`forge review` is expected to fail with these findings. This is an intentional
release hold, not a passing review obtained by accepting required work as a limit.

The original 91-test audit is historical and does not authorize this release.

- Base Git revision: `9617025a134c36f3606931dcf356516b9323546a`.
- Exact 130-file uncommitted candidate SHA-256: `9572f5ed6deac053fb50291b294d516b3e5523e86ab2a139a4adbe7d73f8ad12`.
- Manifest: `reports/candidate-manifest.json`. No user changes were reset and no commit was created.
- Reports/review decisions are excluded from the source hash to avoid circularity.

## Verified evidence

| Check | Result and scope |
| --- | --- |
| Fresh Python 3.12 locked source-copy verification | Passed install, Ruff format/lint, strict mypy, pytest and pure-branch gate; `reports/clean-verification.json` |
| Automated tests | 195 passed; no failures, errors or skips in the fresh environment |
| Pure branch coverage | 697/814 = **85.63%**; distinct from combined **92.36%** |
| Harness control suite | 7/7 deterministic controls |
| RAG control suite | 60 cases, Recall@5=1, MRR@10=1, source-membership citation precision=1, unsupported-answer rate=0; not semantic business quality |
| Retrieval microbenchmark | 5,000 chunks, 100 requests, 10 concurrency, FTS5 + deterministic in-memory vectors; latest p95 **358.327ms** |
| Real OMLX controlled qualification | Earlier full run: 20/20 echo tool, 20/20 intent, 20/20 generated label images, 10/10 embedding, 30/30 reranker; not realistic screenshot/domain qualification |
| Live local platform drill | 30/30 ingest jobs, one additional outage-time job recovered, 20/20 requests after API termination; readiness p95 **13.055ms**, upload acceptance p95 **5.579ms** |
| Reviewer | Both deterministic arms 60/60, zero quality improvement; default-off |
| Edge browser smoke | Public README upload/polling/attachment/citations rendered without console errors; subsequent source-binding/trace changes have API regression coverage, not a second claimed browser run |
| Locust saved CSV snapshot | 1,821 requests / zero failures, 10 users, configured 30 seconds, single keyless API/growing small corpus; not Qdrant or OMLX throughput |
| Runtime dependency audit | No known vulnerabilities at audit time for locked runtime/dev/platform/benchmark; optional all-extras NLTK advisory retained separately |

All result files are under `reports/`. The latest live reports predate this exact
source freeze. The fresh offline report is source-hash-bound; an older live
report's Git revision cannot prove which uncommitted source it executed.

## Correctness fixes and reasoning

1. Knowledge-answer memory retrieval bypassed the approval filter. Both paths now
   use SemanticMemoryService; failed vector deletion cannot re-expose revoked memory.
2. Attachment hashes were concatenated into query text rather than resolved.
   Selected sources now must exist and be indexed before message persistence;
   first-five-chunk excerpt scope is disclosed.
3. RAG IDs now correspond to persisted responses and real request, retrieval,
   model, and completion/failure events. Reopening storage and trace tampering
   are tested.
4. Processor errors now reach ARQ retry scheduling. Storage failures become failed
   jobs, and interrupted processor work can be replayed. This does not demonstrate
   crash-atomic external transactions.
5. Actual CodingAgent tests demonstrate two successive writes require two
   approvals, clients are closed, and restart does not restore old capability.
6. The combined coverage score had concealed insufficient pure branch coverage.
   Independent calculation and negative review tests now prevent that substitution.

## Architecture and safety

The runtime owns tool permissions, budgets, exact approvals and checkpoints.
RAG responses are separate from coding checkpoints. Memory SQL status remains
authoritative after a remote cleanup failure; knowledge and memory vector
families are separate. Session histories are separate, not tenant-secured.

See [SECURITY.md](SECURITY.md) for rule IDs, line evidence, fixes and residuals.
Local Host/Origin checks, hashed CSP and textContent are defense in depth.
No OS sandbox, public authentication, full privacy classifier, signed trace root,
or production HA is claimed. Public reports use synthetic/public fixtures.

## Required continuation

1. FH-015: integrate bounded conversation/history/knowledge/memory context and
   summaries, finish ordinary chat/memory-command run lifecycle, and persist
   response citations/task state.
2. FH-016: improve real-model fixtures/evaluator validity. Source membership is
   not entailment; generated labels are not realistic diagrams; median latency
   is not first-token latency or tokens per second.
3. FH-017: complete remaining platform/framework experiments and bind every final
   live report to frozen source. PostgreSQL currently stores sessions/messages;
   other metadata remains shared SQLite, not multi-host storage.
4. FH-018: expand the personal 200-question outline and complete executable
   beginner lessons and actual mock interviews. The new first lab teaches
   attachment identity, trace verification and HTTP-vs-task correctness.

Only after the required items are resolved should the final audit become
`passed`. Machine-readable decisions: [FINDINGS.json](FINDINGS.json).

## Re-triage against the current source — 2026-11-19

This section does not re-run the 2026-09-04 audit and does not change
`FINDINGS.json`, which stays bound to candidate `9617025` until a new frozen audit
candidate is reviewed. It records where each open finding stands in the source after
0.14.0, so the open list is not read as current without context.

| Finding | Status in current source | Recommendation |
| --- | --- | --- |
| FH-015 | Partly resolved. General chat, memory proposals and the "no workspace" coding reply now persist a run and a hash-chained trace, so every response ID resolves through `/v1/runs/{id}` and `/v1/traces/{id}/verify` (`test_conversation_routes_all_workflows`). Still open: token-budgeted history/knowledge/memory context and durable summaries (chat history is bounded by message count only), and persisted session citation/task state. | Keep open, narrowed to the context and session-state items. |
| FH-016 | Unchanged. Every evaluation since, including the DeepSeek studies, uses synthetic tasks; the OMLX vision, TTFT and RAG claim-support items were never run. The README boundary already states that no real business quality is claimed. | Owner decision: keep open, or accept with the README boundary as the residual statement, since the enhanced-release plan it came from is archived. |
| FH-017 | Unchanged for its scope. Hosted execution is PostgreSQL-authoritative since 0.12 with its own frozen candidates (`reports/saas/`), but the local knowledge application still keeps metadata in SQLite and the native full-fault framework comparison is incomplete (`native_full_fault_comparison_complete=false`). | Owner decision, as for FH-016. |
| FH-018 | Out of the repository's scope: interview materials were moved out of the project in `9617025` and are not a source deliverable. | Owner decision: accept as out of scope for the source audit. |

`forge review` keeps failing on purpose until these decisions are made and a new
candidate is frozen; that is the completion rule in `AGENTS.md`, not a defect.

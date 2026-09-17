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
| Keyless Agent Benchmark foundation: frozen 61-case set across 8 categories with a dev/holdout split; a unified evaluator drives **both** runtimes and reports benchmark cases and 7 scorer probes separately, grading tool-set accuracy, call sequence, positional argument accuracy, no-tool discipline and budget boundaries | `evals/agent_cases.json`, `evaluation/agent.py`, `tests/unit/test_agent_evaluation.py`, `reports/agent-eval-dev-keyless.json`, `docs/NEXT_PHASE_PLAN.md` |
| Live Agent Benchmark comparison (M8-B): live `equal` and `natural` profiles drive a real model through the same evaluator, with per-case repeated sampling, per-arm budget recording, and profile-decoupled validity verdicts | `reports/agent-eval-dev-equal-omlx.json`, `reports/agent-eval-dev-natural-omlx.json`, `docs/M8B_LIVE_COMPARISON.md` |
| Bad Case capture and diagnosis (M9-B1): a failing sample is persisted as a record bound to its origin and to a hash-chained trace, with structural failure classification (driven by a provider error code, not a message), derived no-progress diagnostics, and `--only-cases` targeted reproduction | `evaluation/bad_case.py`, `reports/bad-cases/`, `tests/unit/test_bad_case.py` |
| Trace step projection (M9-A): one projector turns a verified hash-chained trace into ordered step records with stable `logical_call_id`/`attempt_id`, per-phase attribution (planner vs executor), separate derived model and measured tool latency, budget remainder per step, and explicit `incomplete` resolution for unclosed calls. Shared by the evaluator and `forge trace-steps` | `observability/steps.py`, `tests/unit/test_trace_steps.py`; trace events now carry `call_id` and `budget` |

| Frozen calibration and sampling methodology (M8-C-prep): equal-resource cap is derived by a pre-registered rule (pooled dev-natural P75 of `total_run_steps`) with a `constraint_effective` gate requiring >=10% of runs to bind; holdout sampling fixed at 5 runs per case per arm; reports pin observation/recovery/benchmark/budget versions | `docs/M8C_CALIBRATION_RULES.md`, `constraint_binding`, report version fields |
| M9-B closure: three layered red→fix→green loops (executor protocol, planner protocol, tool execution) with a unified design, documented in one overview | `docs/M9B_OVERVIEW.md` |
| Structured tool-failure observations (M9-B3 #3): tool exceptions are classified into a frozen 4-code taxonomy and rendered as a deterministic, model-facing JSON observation, with error code/exception class kept in the trace; the model no longer receives raw exception text or host paths | `tools/base.py`, `tools/dispatcher.py`, `tests/unit/test_tool_failure_observation.py`, `docs/M9B3_TOOL_FAILURE_OBSERVATION.md` |
| Bad Case replay and two red→fix→green loops (M9-B3): a minimal `RecordedModel` replays a frozen violation so a harness fix can be proven without model randomness; recoverable protocol violations are fed back instead of terminating the run — executor multi-call violations bounded by `RunBudget.max_protocol_violations`, planner violations by `RunBudget.max_planner_retries` | `evaluation/replay.py`, `runtime/loop.py`, `runtime/plan_execute.py`, `tests/unit/test_replay.py`, `tests/unit/test_plan_execute.py`, `docs/M9B3_RED_FIX_GREEN.md`, `docs/M9B3_PLANNER_RECOVERY.md` |

## M10-C implemented (soft run deadline)

| Area | Evidence |
| --- | --- |
| `RunBudget.max_wall_seconds`: a **soft / admission** run deadline, checked between steps, reported as `FAILED` with `error_type=deadline_exceeded`, and explicitly never implemented by cancelling an in-flight tool | `runtime/budget.py`, `runtime/loop.py`, `tests/unit/test_run_deadline.py`, `docs/M10C_RUN_DEADLINE.md` |

- The load-bearing property is *where* the deadline stops a run. Aborting a tool mid-write would leave an unknown side-effect extent — precisely the state the invocation journal cannot resolve — so a tool that has begun is allowed to finish and be journaled, and the deadline only prevents new work. Two tests assert this: the tool executes exactly once, the invocation settles `COMPLETED` **with** its result, and the run still ends at the deadline.
- **It is not a hard wall-clock bound.** A run may finish later than `max_wall_seconds` by up to one tool's own timeout, because an in-flight tool is never preempted. Do not describe it as a guarantee that a run cannot exceed the ceiling.
- The clock is per invocation, not per task: a run resumed after a crash gets its own allowance rather than inheriting an expired one.
- `clock_fn` and `sleep_fn` are injectable, so no test waits in real time.
- The service sets an explicit ceiling (`CODING_RUN_DEADLINE_SECONDS = 1800`) for HTTP-backed runs, so the duration bound does not move when step or timeout defaults change. The **evaluation profiles deliberately set none**: the frozen calibration rule gives the equal profile exactly one main binding constraint (total steps), and a second one would make a failure unattributable.

## M10-B implemented (retry, attempts, graded timeouts)

| Area | Evidence |
| --- | --- |
| Retry decision matrix as a single pure function separating *agent-recoverable* from *runtime-retryable*; `tool_attempts` records physical executions separately from logical invocations; deterministic backoff; per-tool `timeout_seconds` on `ToolSpec` | `runtime/retry.py`, `state/invocation_journal.py`, `tools/base.py`, `tests/unit/test_retry_decision.py`, `tests/unit/test_tool_retry.py`, `docs/M10B_RETRY_AND_ATTEMPTS.md` |

- **The safety invariant of this milestone:** `non_idempotent` + `timeout` is **never retried**, because the tool body ran and the runtime cannot know whether the effect landed; it settles `indeterminate`. Asserted cell by cell across the whole budget, plus an end-to-end test proving the tool body is entered exactly once.
- **"Recoverable" no longer means two things.** `decide_retry()` does not accept the agent-facing `recoverable` flag at all, so `path_not_found` (which the model can recover from by choosing differently) is never auto-retried with the same arguments. That is the test that keeps agent recovery and runtime retry apart.
- Retry is bounded and deterministic: `max_attempts_per_call=3`, backoff 100/200/400ms capped at 1000ms, **no jitter** (benchmark, replay and latency evidence need reproducible timing; a single local client has no thundering herd). Tests inject `sleep_fn`, so no test sleeps in real time.
- An attempt's failure does not settle the invocation: only the final attempt does. Otherwise `failed` would degrade into "one attempt failed", which recovery relies on meaning "this call did not happen". The terminal state now comes from the retry decision rather than being re-derived, removing the second source of truth that inferred it from dispatcher metadata.
- The frozen metric contract now has a live implementation: one `logical_tool_call` can produce N `tool_attempts`, and every attempt of a call carries the **same** `idempotency_key` (regression-tested, since a regenerated key would make idempotent replay a false claim).
- **Not implemented, by design:** circuit breaker, adaptive retry, retry queue, background scheduler (a single local process has nothing to break-circuit and no queue to schedule).

## M10-A implemented (with stated boundary)

| Area | Evidence |
| --- | --- |
| Tool Invocation Journal (ADR 009): a durable record per logical call as the source of truth for tool side effects, with `claimed → started → completed / failed / indeterminate`, `started` committed before any side effect, fail-closed recovery for non-idempotent calls, and a frozen three-way effect class defaulting to the safe value | `state/invocation_journal.py`, `runtime/loop.py`, `tests/unit/test_invocation_journal.py`, `docs/M10A_INVOCATION_JOURNAL.md` |

- Two claims are proven by determinism tests, not by argument: a completed side effect is not executed twice when the checkpoint that would have recorded it was lost (Test A), and a non-idempotent call found at `started` is never replayed (Test B). Test B is split into B1 (no side effect yet — the deliberate false positive) and B2 (side effect occurred — the real duplicate prevented).
- **Recovery now has a real entry point** (M10-A.1): `RecoveryCoordinator` orchestrates a scan of mid-flight runs and delegates every safety decision to `resume_running`, and `forge recover <workspace> [--run-id ID]` is the working production command. Concurrency is handled by the existing locks (the journal's uniqueness on `(run_id, logical_call_id)` and the checkpoint's optimistic revision), so two processes starting at once cannot both recover one run; the loser reports `conflict`.
- **Service-restart discovery is now wired** (M10-A.2). A thin global `TaskRegistry` records `task_id → workspace` (relative to the configured root, canonicalised before persisting and re-validated on every read, so a tampered entry cannot direct recovery at an arbitrary directory). It holds **no** authoritative status — the checkpoint remains the only status authority. The API scans once during startup, before serving, and only when `recover_on_startup=True` (default off); scanning only at that moment is what avoids mistaking a live task for a crash without introducing a lease. Verified end to end: a real app boots, finds a leftover task through the registry, and refuses to replay its unconfirmable non-idempotent write while still serving.
- The coordinator targets **single-process leftover RUNNING runs** and does not claim multi-worker failover. heartbeat, distributed lease, leader election, and worker ownership are explicitly out of scope.
- `recovery_semantics_version` is now **1**. The delivery guarantee is **not** described as at-most-once: non-idempotent side effects use fail-closed recovery, while read-only and genuinely key-honouring tools may be replayed safely.
- `tool_attempts` is deliberately **not** created; `tool_invocations.attempt_count` is not a physical execution log, and M10-B will add separate attempt records when retry is designed.

## Earlier design notes

- **ADR 009 — Tool Invocation Journal and Recovery Semantics** is written but **not implemented**. It freezes: the journal as the source of truth for tool side effects (the checkpoint owns control flow, the trace is evidence only and must never be read as "the tool did not run"); a three-way `effect_class` (`read_only` / `idempotent` / `non_idempotent`, defaulting to the safe one); a `claimed → started → completed / failed / indeterminate` state machine whose `started` write is durably committed **before** the tool is called; a recovery decision matrix; a two-layer schema (`tool_invocations` for logical calls, `tool_attempts` for physical executions, the latter declared but unused until M10-B); and four fault-injection tests, of which the `started` crash window is the load-bearing one. It explicitly does **not** claim exactly-once delivery.
- `effect_class` is assigned conservatively and `write_file` is deliberately **not** declared `idempotent`: its optimistic `expected_sha256` check makes a repeat fail rather than duplicate, which is not idempotency under a key. Declaring it otherwise requires proof that the same key plus the same content produces no second effect.
- Recovery must reconstruct the *conversation effect* (append the recovered observation as a tool message), not merely avoid re-executing the tool.

## Frozen holdout evaluation (M8-C, final)

| Area | Evidence |
| --- | --- |
| The holdout set has now been run once, after the experiment identity was frozen from live code constants, with 5 samples per case per arm on both profiles | `reports/agent-eval-holdout-natural-omlx.json`, `reports/agent-eval-holdout-equal-omlx.json`, `reports/freeze-manifest.json`, `docs/M8C_HOLDOUT_REPORT.md`, `docs/M8C_FREEZE_RECORD.md` |

- Both reports match the freeze manifest on revision, benchmark, observation-contract, recovery-semantics and budget-profile versions, holdout id hash, and sample count. The holdout set was **unused before this run**; all tuning happened on dev.
- **Result: success rates are effectively equal (ReAct 11/14, Plan-Execute 10/14), with 11 of 14 cases identical between the two.** The three differing cases do not point one way (two favour ReAct, one favours Plan-Execute).
- **The cost difference is the clear finding:** at near-identical success, Plan-Execute's median steps are ~2x ReAct's (4.0 vs 2.0), input tokens ~1.7x (1906 vs 1134), and latency ~1.5x (13.2s vs 8.6s), with almost non-overlapping IQRs.
- The equal profile's shared cap of 4 bound 39.3% of runs, but it **changed no success**: the three cases it altered were already failing 0/5 under `natural`, so the cap only compressed their step counts and relabelled the reason as exhaustion. This **does not reproduce** the dev-time impression that the cap penalises Plan-Execute success.
- **Limits that must accompany any figure:** one local 9B model, a self-built 14-case holdout (not a public benchmark), 14 cases × 5 samples (no significance testing), single-machine local inference, and no monetary conversion of token counts.
- The abort rule was **not** triggered: no infrastructure defect appeared, and the run completed. Its wording is frozen in [`M8C_CALIBRATION_RULES.md`](M8C_CALIBRATION_RULES.md) §5.1.

## Remaining full-plan gates

- **Two** red→fix→green loops are complete. (1) Executor multi-call protocol violation (`retr-two-searches`): reproduced deterministically pre-fix, fixed, verified green by recorded replay and by live re-evaluation (0/1 → 1/1). (2) Planner protocol violation: bounded correction retry with the parser's strictness **unchanged**, verified red/green/negative on one frozen response, and live-confirmed that the real model corrects the *format* while plan *content* remains model quality. A controlled A/B (same model, same budget, only the retry switch varied) shows **+1 and zero regressions**. The third planned loop — structured tool-error observations — is **not** done. These are proven on specific cases, not general guarantees.
- The protocol-violation fix is **bounded and conservative**: only codes in `RECOVERABLE_VIOLATION_CODES` are retried, the limit defaults to 3, an exhausted limit fails with `repeated_protocol_violation`, and the harness never chooses an action on the model's behalf (a rejected response admits and dispatches nothing). Whether a live model actually corrects itself is model quality, not a runtime guarantee: the runtime's obligation is to give a usable correction opportunity and record the violation faithfully.
- Planner correction is bounded and narrow: only protocol failures retry (`missing_plan_json`, `invalid_plan_json`, `plan_schema_violation`, `planner_called_tool`). A plan that is well-formed but poor is **not** retried — that is model quality and the harness must not silently "fix" it. The parser's acceptance criteria were not widened: malformed JSON is still rejected, only the diagnostics changed. Each planner attempt is charged in full, so a recovered run stays visibly more expensive than one that succeeded first time.
- A live planner that keeps breaking the protocol still ends the run (after the bounded retries), and no tool is ever dispatched in that case.

- **The equal-resource cap is now derived and binding.** Applying the frozen rule to the post-M10-B dev-natural distribution (pooled n=68, P75=4.00) yields **cap=4** for both arms, binding **29.4%** of runs (`constraint_effective: true`). It is the first time the equal profile differs from natural, so it now answers the question it exists for.

| profile | ReAct | Plan-Execute |
| --- | --- | --- |
| natural | 20/34 (steps 2.24) | 18/34 (steps 3.79) |
| equal (cap=4, shared) | 20/34 (steps 2.24) | **16/34** (steps 3.32) |

- Two cases changed, both Plan-Execute, both exhausted at the shared ceiling: `long-alternate-list-read` and `recover-notfound-then-alternate` passed with a generous budget and fail when four steps are shared. Plan-Execute spends one of those four on planning, leaving three to act; ReAct is unaffected because it never needed more than four.
- **This is a trend on one model, 34 cases and 3 samples — not a significance claim.** The defensible statement is that under a shared 4-step ceiling Plan-Execute loses execution capacity to its planner decision while ReAct does not, not that Plan-Execute is worse in general.
- The M10-B dev-natural regression is **identical** to the pre-M10-B run (same pass counts and mean steps), which means **this benchmark run did not exercise the retry path at all** (no transient failures occurred). Retry correctness rests on the unit-test matrix, not on the benchmark.
- **M8-B live comparison is measured but narrow.** Two live dev reports exist (`reports/agent-eval-dev-equal-omlx.json`, `reports/agent-eval-dev-natural-omlx.json`): 34 cases, 3 samples, one local model (`Qwythos-9B-v2-8bit-mlx`). Result: success rate is equal (19/34 both arms) while Plan-Execute costs ~1.1 more decisions and ~27% more input tokens per case. These are trend observations on a small sample with a single model, **not** a general claim about the two strategies, and no significance testing was done. `long_horizon` (ReAct 0/5, Plan-Execute 2/5) and `retrieval_then_tool` (2/4 vs 1/4) show opposite directions.
- The **equal-resource and natural-cost profiles produced identical results**, because the equal ceiling (12 steps) never bound: the longest run was 7 steps and no case stopped on budget exhaustion. The two profiles therefore cannot yet be distinguished; tightening the ceiling into the observed range, or using longer tasks, is outstanding.
- **The holdout split has still not been run live**, so no holdout result is authorized and the split remains uncontaminated. Holdout is deliberately excluded from `make gate`.
- **Live runs are resource-intensive on this host.** An unbounded per-request output ceiling previously froze a 24 GB machine; `live_eval_config` now bounds it and `--limit` marks a truncated run `complete_split: false`, which forces `measurement_valid: false`. A truncated run must never be cited as a measurement.
- M8-B surfaced and fixed four real harness defects, each with a regression test: loopback requests sent through the system proxy (502); over-strict planner response parsing that penalised Plan-Execute for Markdown formatting; a step-ceiling assertion that made `max_steps=1` no-tool cases structurally unpassable for Plan-Execute; and a wiring contract that reported a bad live plan as a broken harness rather than as a model-quality failure. Live profiles now exclude scorer probes and `budget_boundary` cases, which are unsatisfiable outside the keyless profile.
- **Automatic retry/backoff is not implemented** and is reserved for M10. #3 deliberately stops at classification plus structured feedback; `ToolErrorCode` and `recoverable` are its prerequisites, not a retry policy.
- Tool-failure observation form is an experimental variable: a controlled test showed the same case/model/budget running 4 steps with the pre-#3 raw string versus 11 steps with structured feedback (the model kept probing non-existent paths). The structured form is correct — it is what let a live model change tools — but it changes behaviour, so cross-version comparisons of affected cases must hold the observation form fixed or state the version.
- Bad Case capture exists (M9-B1) and one replay channel is implemented (M9-B3): a failing sample is persisted with its hash-chained trace, and `RecordedModel` replays a frozen violation deterministically. Three Bad Cases are captured, one per layer: a protocol violation (model returned multiple tool calls), two planner failures (a planner that answered with XML only, and one whose JSON had a stray character), and an execution-loop case that looks like a no-progress loop but is evidence-classified as model quality because its repeated searches used different queries. Remaining observability gaps are recorded and deliberately NOT fixed yet: a recurring tool error surfaced as a raw exception string rather than an actionable observation, and a rejected plan ending the run instead of returning the planner to the model. The `usage.steps=0` gap for a rejected response is closed on the recoverable path (the rejected inference now charges a step); on the non-recoverable path the difference is intended, since no further work is done. One contributing case-design weakness is also recorded (a fixture referencing a directory it does not provide).

- The keyless Agent Benchmark (`forge eval-agent --split dev`) validates the grading contract, metric pipeline, both runtime wirings, and budget interaction only. It does **not** compare ReAct against Plan-Execute, because the scripted model replaces the decision under test; strategy claims require live-model runs. Its token figures are synthetic (`token_source: "synthetic"`, `cost_metrics_valid: false`) and never enter cost metrics. Benchmark cases and the 7 scorer probes are reported separately, never as one pass ratio. The dev/holdout split exists and the holdout floor is enforced, but the holdout set has not been used for a live comparison, so no holdout result is authorized; holdout is deliberately excluded from `make gate` to keep it uncontaminated. Trace step projection (M9-A) exists, but its `logical_call_id`→`attempt_id` mapping is currently one-to-one because retries do not exist yet; M10 must extend, not replace, that identity.
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

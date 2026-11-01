> 2026-11-01：0.13.0 新增[邀请制内部 Alpha 控制面](../guides/CLOUD_CONTROL_PLANE.md)，含 PG 身份/组织/工作空间/部署授权、异步 API、独立 worker 和工作台。以[本轮冻结报告](../../reports/saas/20261101-control-plane/REPORT.md)判断工程资格，历史测试不认证当前源码。生产身份、人民币额度、实际云部署和真实业务仍在后续范围。
> 0.12.0 历史候选已实现[PG 托管串行执行](../guides/CLOUD_HOSTED_EXECUTION.md)，原工程资格读取[历史报告](../../reports/saas/20261030-hosted/REPORT.md)。
> 2026-10-29：最近已验收冻结 0.11.0，独立 [PG 状态首切片](../guides/CLOUD_STATE.md) 通过本批门禁。
> 支持范围和验收读取 `reports/saas/20261029-state/REPORT.md`；尚未接入完整 Harness/平台/真实 provider，M1/GA 未完成。
> 2026-10-28：历史源码候选 0.10.0，新增[邀请制本地工作台](../guides/COMMERCE_WORKBENCH.md)，
> 身份修复与本批候选结果见 `reports/saas/20261028-batch1/REPORT.md`。
> SaaS 已开始 W0/W1 首切片；该历史候选中 PG/跨机接管仍为[事务设计](../guides/SAAS_STATE_CONTRACT.md)。
> 下列 0.9.1 及更早小节保持各自历史版本身份。
>
> 2026-10-27：历史源码候选 0.9.1，新增 [有界模型并行](../guides/PARALLEL_EXECUTION.md)：单 writer、
> provider 跨 run 容量、发送边界、取消/撤权后结算与恢复；schema 3 / recovery 5。
> 最终候选资格与真实模型性能只读 `reports/parallel-execution-20261027/REPORT.md`，
> 历史测试数字和旧 A/B/C 结论不认证本轮实现。SessionService/HTTP/UI 全接线与真实业务仍另验。
>
> 2026-10-26：历史源码候选 0.8.6，新增 [固定专家协作](../guides/COLLABORATION.md) 与 R2 输出 schema。
> 独立候选及本轮结果见 `reports/multi-agent-20261026/REPORT.md`；完整路线见
> [执行计划](EXECUTION_PLAN_MULTI_AGENT.md)。串行、一次决策、只读；后续批次另验。
> 下文 0.8.5 与更早状态、通过数字均保留历史身份。
>
> 2026-10-26 当前状态索引：[能力与宿主兼容矩阵](../CURRENT_CAPABILITIES.md)。
> 当前源码候选为 0.8.5：新增每次 kernel 模型调用工具目录控制并修正评测接线；R2 能力沿用 0.8.3。
> 新候选 `reports/improvement-20261026/candidate-r3/` 独立冻结验证，宿主原 wheel 不自动迁移。
> 求职改进执行边界与结果以 `reports/improvement-20261026/REPORT.md` 为准；
> 下列各历史小节的待办和通过数字只属于各自记录的源码，不覆盖 R2 后续能力。
>
> Remaining work and acceptance plan (updated 2026-10-21; baseline 2026-10-20):
> [剩余完善与验收计划书](REMAINING_WORK_PLAN_2026-10-20.md).
> This is a plan, not a qualification of the current source.

# 0.8.3 R2 engineering candidate (2026-10-23)

The opt-in [R2 explicit graph](../guides/R2_ORCHESTRATION.md) has durable node facts, atomic
approval admission, reservations, shared tool attempts, unknown model/effect
reconciliation, deterministic ready branches and joins, local owner fencing,
cancellation and a transactional event outbox. New R2 checkpoint v2 / recovery v4
is rejected by old installations; existing ReAct v1 / recovery v3 remains intact.
The arbitrary one-shot custom kernel remains read-only and non-resumable.

Qualification is read from `reports/release-0.8.3-r1/verification/summary.json`;
source, clean-copy, dependency, wheel and R2 subprocess gates bind that candidate.
No production, actual-model or host business-transaction claim follows. Host
run/job/session mapping, real writes and formal HTTP/UI integration remain separate.
Implementation review is self-review, not independent professional review.
The 0.8.0-r1 candidate failed six release fixture/allowlist tests; its complete
failed report and artifacts remain immutable. The 0.8.1 candidate synchronizes
those tests with the new public three-process demo without relaxing checks.
Its 92 automated gates passed, but post-gate self-review reproduced mutable nested
arguments/resource revisions/budget changing during an active run (R2-005).
It is superseded, not a final release. The 0.8.2 candidate revalidates the complete
frozen identity at every admission/settlement and adds three regression cases.
The 0.8.2 automated gates also passed, but post-gate R2-006 reproduced a zero-step
node receiving a one-step nested allowance. It is superseded. The 0.8.3 executor
accepts an exact node step limit, including zero, without weakening RunBudget's
positive run-step invariant; zero/positive nested allowance regressions are added.

## Previous 0.7.1 engineering candidate

ADR 030 extracts shared tool invocation mechanics for ReAct and the read-only
kernel. Kernel model/tool budget settlement is serialized and nested usage is
accounted. Fresh candidate evidence belongs to `reports/release-0.7.1-r1/`;
qualification must be read from that candidate's actual verification result.
Custom orchestration write, approval and resume remain unsupported pending R2.
The section below records the previous 0.6.0 candidate, not the new source.

## Previous 0.6.0 release candidate (2026-10-21)

The 0.5.0 base and the accumulated Unreleased framework changes are now versioned
0.6.0. The label resolves the same-day conflict between two different artifacts
that both claimed 0.5.0: the commerce-bound `58579893…` wheel qualified under
`reports/request-preflight-2026-10-17/` is unaffected, and the `8275baba…` wheel
from the framework-next run stays historical evidence. The six pip-audit failures
of that framework-next run are fixed in the lockfile: aiohttp 3.14.5,
certifi 2026.10.19, pillow 13.0.0 (the `knowledge`/`cli` bound moves to
`>=13,<14`) and transitive yarl 1.25.1; the NLTK FH-019 exception is unchanged
and no new waiver was added. The comparison protocol's declared forge arm is
re-declared as 0.6.0 and re-sealed without touching scenarios or grading. The
first 0.6.0 freeze and its 83/87 attempt stay in `reports/release-0.6.0/` as
diagnostics: its four remaining pip-audit failures came from the two independent
comparison-environment lockfiles still pinning certifi 2026.7.22, which the root
lock upgrade cannot reach. After upgrading those locks (certifi 2026.10.19), the
corrected candidate is frozen as `reports/release-0.6.0-r1/`; only its fresh
verification summary can establish qualification, and the older freezes keep
their own evidence. F1c-2 (custom-orchestrator write/approval/resume) remains
pending.

# Baseline hardening in progress (2026-10-21)

R0 now has a workspace-scoped candidate directory option, separate freeze-only and
verification commands, and matching CI artifact paths. Historical freezes remain
unchanged. The current collected suite before the new gate regressions is 1116;
the old pytest nodeids cache contains seven additional stale names (1123 total).
The number in the F1c-1 execution note is corrected accordingly. Whole-tree
formatting and current-versus-historical documentation have been reconciled.
See [current verification](BASELINE_VERIFICATION.md) for the entry point and evidence
locations. Only that candidate's fresh summary can establish qualification; this
implementation note does not report that every gate has passed. F1c-2 remains pending.

# Shared governed model invocation and SDK module split (2026-10-20)

`runtime/model_call.py` now owns the single governed model call
(`invoke_model_once`): preparation, request budgeting, the canonical trace sequence and
all token accounting live in one place, consumed by both `AgentRuntime._drive` and
`RunKernel.call_model`. Accounting can no longer drift between orchestration styles: a
recoverable protocol violation charges one step plus the rejected request's tokens on
both paths, and the kernel surfaces the stable violation code
(`model protocol violation: {code}`) instead of dropping it. Loop policy (violation
feedback, limits, termination) stays with each caller; `plan_execute.py` keeps its
separate planner flow. `sdk/application.py` (~970 lines) is split into `sdk/spec.py`,
`sdk/kernel.py` and `sdk/harness.py`, with the old path kept as a 30-line re-export shim
until API v2 and the private bridges renamed `ModelBridge/ToolBridge/TraceBridge`.
Write/approval tools remain ReAct-only (ADR 026); the shared tool executor and custom
run suspension/resume are F1c-2. See ADR 029.

# Application composition audit (2026-10-20)

`compose_application` now records an `ApplicationComposition` (ordered capability names
plus a structural digest over capability/tool names, instruction texts and provider
types) on the `ApplicationSpec`, and every composed run emits an
`application.composition` trace event before context compilation. The digest is a
composition-shape identity, not an implementation hash; behavior changes still require
an application version bump. All reference apps (legal pilot, LexVault DAG, commerce,
commerce readonly, consultation, records, coding) now compose through capabilities;
direct `ApplicationSpec` construction remains supported and yields
`composition=None`. See ADR 028.

# Bundle citation integrity and export snapshots (2026-10-20)

The LexVault read-only path now carries per-page `text_sha256` (auto-derived,
mismatch-on-supply rejected), a content-addressed `CaseBundle.snapshot_digest`, and a
citation verifier that accepts `[page:N]「exact quote」` with verbatim-substring
checking plus an optional `strict_quotes` mode; page digests are recomputed at
verification time so a mutated bundle fails closed. The exporter reads inside one
explicit read transaction, stores page text verbatim, and binds the default version
to page digests. The import-boundary scan now also detects `import_module("...")`
string literals and found a third frozen Coding shim (`context/repository.py`); the
allowlist is corrected to three files. Semantic support and legal correctness remain
unassessed. See ADR 027.

# Capability composition and orchestrator kernel (2026-10-18)

`Capability` and `compose_application` compose an application from ordered, trusted
tools/instructions/context providers while keeping policy, verifier, budget and
authorization explicit. Existing `ApplicationSpec` construction is unchanged. See
ADR 022. `Orchestrator` and `RunKernel` are also implemented for application-defined
control flow: model calls, read-only tool calls, budget, trace, checkpoints and the
verifier stay governed. Custom runs are one-shot; write/approval/journal/recovery paths
remain ReAct-only. See ADR 023. The legal case-QA app now composes its tools through
`Capability`; `tests/unit/test_import_boundaries.py` enforces the core ->
application/coding boundary, with two API-v1 Coding shims frozen for API v2. See
ADR 024. F2a adds a synthetic LexVault-shaped read-only case bundle, adapter and
parallel specialist DAG with a `[page:N]` verifier (ADR 025). F2b adds a read-only
LexVault-schema SQLite exporter that preserves document/page identity, plus a
four-question test that verifies the source database hash is unchanged. A one-off
read-only run on the local case 1 (14 pages, 5 documents) completed four questions with
a scripted model and an unchanged source hash; real model/lawyer quality is not claimed.
This is still not a production LexVault/UI integration. The commerce reference apps now
compose through `Capability` too. `tests/unit/test_application_conformance.py` checks the
application spec/tool/order/wrong-binding contract across legal and commerce;
`tests/unit/test_runtime_conformance.py` runs a shared runtime subset for legal DAG and
commerce (success/trace/inspect, verifier rejection, wrong binding, missing-resource
fail-closed). Approval/resume/budget permutations are not part of that shared table.
`docs/guides/CONFORMANCE.md` records the remaining gaps. Custom-orchestrator write/approval
tools remain ReAct-only (ADR 026); capability-contributed policy/verifier composition
remains future work.

# 请求预算与电商只读接入（2026-10-17）

已实现规划/执行阶段的完整 prepared-input 预检、不可变 JSON 发送及 trace 明细。
提供电商单商品只读工厂与 keyless 示例，要求宿主当前授权，复用资源策略和台账验收。
设计见 ADR 020/021，范围见 [请求预检](../guides/REQUEST_PREFLIGHT.md) 与
[只读接入](../guides/COMMERCE_READONLY.md)，审查见 [REQUEST_PREFLIGHT_AUDIT](../review/REQUEST_PREFLIGHT_AUDIT.md)。
新源码候选与门禁位于 `reports/request-preflight-2026-10-17/`；通过与否以其中冻结和验证摘要为准。
默认计数仍是启发式，不是真实 tokenizer 上界；真实店铺、模型质量与生产部署未验收。

# Historical: 源码借鉴与资料预算修复（2026-10-16）

本轮阅读四个官方项目的固定提交，修复 ContextCompiler 漏算来源标签和分隔符的问题。
范围与边界见 [审查记录](../review/UPSTREAM_DESIGN_REVIEW.md) 和
[ADR 019](../adr/019-rendered-material-budget.md)。新源码证据位于
`reports/upstream-design-study-2026-10-16/candidate-source.json` 与 `verification/`；
是否通过以其中的冻结和验证摘要为准。旧 P0/P1 与业务报告保留为历史证据。
完整请求预算、SDK 简化和工作区后端仍是建议，真实业务未验收。

# Historical: P0/P1 开发状态（2026-10-16）

当前进入对照验证首轮，范围见 [next-phase-scope](../../reports/next-phase-scope.md)。
新源码的工程验收以 `reports/next-candidate/` 中的冻结和验证为准；下文 0.5.0 报告保留为历史证据。
原生完整故障对照、独立接入体验和真实业务验收仍未完成，不能以共同宿主组合结果替代。

# Implementation status

## Historical: 0.5.0 local synthetic candidate (2026-10-15)

See [BUSINESS_PILOTS.md](../guides/BUSINESS_PILOTS.md) for implemented S2–S4 local flows,
S1 hardening, S5 synthetic contracts and remaining business work. The
`reports/business-release-audit.json` and frozen verification qualify only their recorded source identity.
The sections below retain historical platform/candidate evidence; their test counts
and statements about pending work do not describe the entire current SDK.


## Durable scoped session scope (0.4.0 candidate, 2026-10-14)

An embeddable session layer is implemented over the existing public controllers:
`SessionService` persists conversations, ordered turns, stable request keys and run
bindings in its own SQLite schema, executes through `Harness`/`WorkflowRunner`,
and re-checks current host authorization for every operation. An optional FastAPI
adapter (`session_router`) is importable without making the base SDK depend on Web
extras. The public contract is documented in [SESSIONS.md](../guides/SESSIONS.md); the design
record is [ADR 014](../adr/014-durable-scoped-sessions.md). A keyless synthetic client
for both a legal-style and a commerce-style conversation, including restart,
delegated approval and revocation, is `examples/session_demo.py`.

Candidate qualification is recorded in `reports/session-release-audit.json` and its
frozen source evidence (`reports/session-candidate-source.json`,
`reports/session-verification/`), not inherited from the 0.3.0 report; the review
record is [SESSION_AUDIT.md](../review/SESSION_AUDIT.md). The suite has
699 deterministic tests and holds the pure-branch gate (>=85%); the measured figures
are in that audit. Everything here is synthetic and local: no real model quality,
tenant isolation guarantee, legal or commerce business result is claimed.

Phase S1 is the first delivered slice of the next-phase plan
(`reports/MODULAR_NEXT_PHASE_PLAN.md`). S2 versioned import and durable tasks,
S3 LexVault integration, S4 the commerce MVP and S5 frozen business evaluation are
**not** delivered. Legacy enhancement findings FH-015–FH-018 remain open as recorded
below; this phase does not close them. The session layer keeps the last finalised
0.3.0 behaviour and SDK API v1 unchanged and adds no legacy data migration.

## Modular business SDK scope (0.3.0, 2026-10-12)

Workflow composition, scoped reviewed revisions, source dependency invalidation,
exact-span citations and workflow completion verification are implemented; see
[BUSINESS_MODULES.md](../guides/BUSINESS_MODULES.md). Legal note and commerce consultation/content
fixtures use public SDK contracts. Current candidate qualification is recorded in
`reports/modular-release-audit.json`, not inherited from the 0.2.0 report.
LexVault migration, full commerce UI/channels, generic conversation/jobs/cancellation,
parallel DAG scheduling and real-domain model evaluation remain future work.
Historical platform audit findings below remain separate and open as recorded.

## Framework SDK scope (0.2.0)

The abstract framework delivery is tracked separately in [FRAMEWORK_DELIVERY.md](FRAMEWORK_DELIVERY.md).
Public SDK, scoped local execution, durable atomic approval, recovery generation 3,
ordered persisted verification evidence and two reference applications are implemented.
The historical enhanced knowledge/platform audit below remains open; its reports do
not certify the new framework candidate and the framework does not close FH-015–017.

## Historical enhanced candidate scope

This file is the source of truth for the enhanced candidate. **The full user-approved enhancement plan is not yet complete.** The earlier `e3a8a38...` audit covered only the original Harness release and must not authorize the new feature set. The current audit records remaining implementation and evidence gaps explicitly; a passing unit suite is not a final release decision.

## Implemented and tested

| Area | Evidence |
| --- | --- |
| Durable scoped sessions (0.4.0 candidate): application/tenant/actor/resource binding, stable request keys, bounded untrusted history, single-writer turns, delegated approval with separately authenticated approver, crash reconciliation, tombstones and session retirement | `sdk/sessions.py`, `tests/unit/test_sessions.py`, `docs/guides/SESSIONS.md`, ADR 014 |
| Optional host-authenticated session HTTP adapter; identity never read from request JSON; 401/403/409/422 separation | `sdk/session_control.py`, `tests/unit/test_session_control.py` |
| Session qualification gate: candidate freeze, independent clean-copy re-verification, wheel/sdist membership, minimal-wheel install | `benchmarks/session_verification.py`, `tests/unit/test_session_verification.py`, `reports/session-release-audit.json` |
| Original Agent Loop, planning, tools, policy, approval, checkpoint, context, Skills, MCP, Sub-agent, memory and hash trace | Existing unit/integration tests under `tests/` |
| Harness-owned completion verification gate before `succeeded`; the coding vertical requires a successful write and passing tests | `coding/verification.py` (legacy import shim retained), runtime gate tests in `test_runtime.py`, `test_verification.py`, `test_coding_agent.py` retry scenario, ADR 006 |
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
| Keyless Agent Benchmark foundation: frozen 61-case set across 8 categories with a dev/holdout split; a unified evaluator drives **both** runtimes and reports benchmark cases and 7 scorer probes separately, grading tool-set accuracy, call sequence, positional argument accuracy, no-tool discipline and budget boundaries | `evals/agent_cases.json`, `evaluation/agent.py`, `tests/unit/test_agent_evaluation.py`, `reports/agent-eval-dev-keyless.json`, `docs/archive/NEXT_PHASE_PLAN.md` |
| Live Agent Benchmark comparison (M8-B): live `equal` and `natural` profiles drive a real model through the same evaluator, with per-case repeated sampling, per-arm budget recording, and profile-decoupled validity verdicts | `reports/agent-eval-dev-equal-omlx.json`, `reports/agent-eval-dev-natural-omlx.json`, `docs/archive/M8B_LIVE_COMPARISON.md` |
| Bad Case capture and diagnosis (M9-B1): a failing sample is persisted as a record bound to its origin and to a hash-chained trace, with structural failure classification (driven by a provider error code, not a message), derived no-progress diagnostics, and `--only-cases` targeted reproduction | `evaluation/bad_case.py`, `reports/bad-cases/`, `tests/unit/test_bad_case.py` |
| Trace step projection (M9-A): one projector turns a verified hash-chained trace into ordered step records with stable `logical_call_id`/`attempt_id`, per-phase attribution (planner vs executor), separate derived model and measured tool latency, budget remainder per step, and explicit `incomplete` resolution for unclosed calls. Shared by the evaluator and `forge trace-steps` | `observability/steps.py`, `tests/unit/test_trace_steps.py`; trace events now carry `call_id` and `budget` |

| Frozen calibration and sampling methodology (M8-C-prep): equal-resource cap is derived by a pre-registered rule (pooled dev-natural P75 of `total_run_steps`) with a `constraint_effective` gate requiring >=10% of runs to bind; holdout sampling fixed at 5 runs per case per arm; reports pin observation/recovery/benchmark/budget versions | `docs/archive/M8C_CALIBRATION_RULES.md`, `constraint_binding`, report version fields |
| M9-B closure: three layered red→fix→green loops (executor protocol, planner protocol, tool execution) with a unified design, documented in one overview | `docs/archive/M9B_OVERVIEW.md` |
| Structured tool-failure observations (M9-B3 #3): tool exceptions are classified into a frozen 4-code taxonomy and rendered as a deterministic, model-facing JSON observation, with error code/exception class kept in the trace; the model no longer receives raw exception text or host paths | `tools/base.py`, `tools/dispatcher.py`, `tests/unit/test_tool_failure_observation.py`, `docs/archive/M9B3_TOOL_FAILURE_OBSERVATION.md` |
| Bad Case replay and two red→fix→green loops (M9-B3): a minimal `RecordedModel` replays a frozen violation so a harness fix can be proven without model randomness; recoverable protocol violations are fed back instead of terminating the run — executor multi-call violations bounded by `RunBudget.max_protocol_violations`, planner violations by `RunBudget.max_planner_retries` | `evaluation/replay.py`, `runtime/loop.py`, `runtime/plan_execute.py`, `tests/unit/test_replay.py`, `tests/unit/test_plan_execute.py`, `docs/archive/M9B3_RED_FIX_GREEN.md`, `docs/archive/M9B3_PLANNER_RECOVERY.md` |

## M10-C implemented (soft run deadline)

| Area | Evidence |
| --- | --- |
| `RunBudget.max_wall_seconds`: a **soft / admission** run deadline, checked between steps, reported as `FAILED` with `error_type=deadline_exceeded`, and explicitly never implemented by cancelling an in-flight tool | `runtime/budget.py`, `runtime/loop.py`, `tests/unit/test_run_deadline.py`, `docs/guides/M10C_RUN_DEADLINE.md` |

- The load-bearing property is *where* the deadline stops a run. Aborting a tool mid-write would leave an unknown side-effect extent — precisely the state the invocation journal cannot resolve — so a tool that has begun is allowed to finish and be journaled, and the deadline only prevents new work. Two tests assert this: the tool executes exactly once, the invocation settles `COMPLETED` **with** its result, and the run still ends at the deadline.
- **It is not a hard wall-clock bound.** A run may finish later than `max_wall_seconds` by up to one tool's own timeout, because an in-flight tool is never preempted. Do not describe it as a guarantee that a run cannot exceed the ceiling.
- The clock is per invocation, not per task: a run resumed after a crash gets its own allowance rather than inheriting an expired one.
- `clock_fn` and `sleep_fn` are injectable, so no test waits in real time.
- The service sets an explicit ceiling (`CODING_RUN_DEADLINE_SECONDS = 1800`) for HTTP-backed runs, so the duration bound does not move when step or timeout defaults change. The **evaluation profiles deliberately set none**: the frozen calibration rule gives the equal profile exactly one main binding constraint (total steps), and a second one would make a failure unattributable.

## M10-B implemented (retry, attempts, graded timeouts)

| Area | Evidence |
| --- | --- |
| Retry decision matrix as a single pure function separating *agent-recoverable* from *runtime-retryable*; `tool_attempts` records physical executions separately from logical invocations; deterministic backoff; per-tool `timeout_seconds` on `ToolSpec` | `runtime/retry.py`, `state/invocation_journal.py`, `tools/base.py`, `tests/unit/test_retry_decision.py`, `tests/unit/test_tool_retry.py`, `docs/guides/M10B_RETRY_AND_ATTEMPTS.md` |

- **The safety invariant of this milestone:** `non_idempotent` + `timeout` is **never retried**, because the tool body ran and the runtime cannot know whether the effect landed; it settles `indeterminate`. Asserted cell by cell across the whole budget, plus an end-to-end test proving the tool body is entered exactly once.
- **"Recoverable" no longer means two things.** `decide_retry()` does not accept the agent-facing `recoverable` flag at all, so `path_not_found` (which the model can recover from by choosing differently) is never auto-retried with the same arguments. That is the test that keeps agent recovery and runtime retry apart.
- Retry is bounded and deterministic: `max_attempts_per_call=3`, backoff 100/200/400ms capped at 1000ms, **no jitter** (benchmark, replay and latency evidence need reproducible timing; a single local client has no thundering herd). Tests inject `sleep_fn`, so no test sleeps in real time.
- An attempt's failure does not settle the invocation: only the final attempt does. Otherwise `failed` would degrade into "one attempt failed", which recovery relies on meaning "this call did not happen". The terminal state now comes from the retry decision rather than being re-derived, removing the second source of truth that inferred it from dispatcher metadata.
- The frozen metric contract now has a live implementation: one `logical_tool_call` can produce N `tool_attempts`, and every attempt of a call carries the **same** `idempotency_key` (regression-tested, since a regenerated key would make idempotent replay a false claim).
- **Not implemented, by design:** circuit breaker, adaptive retry, retry queue, background scheduler (a single local process has nothing to break-circuit and no queue to schedule).

## M10-A implemented (with stated boundary)

| Area | Evidence |
| --- | --- |
| Tool Invocation Journal (ADR 009): a durable record per logical call as the source of truth for tool side effects, with `claimed → started → completed / failed / indeterminate`, `started` committed before any side effect, fail-closed recovery for non-idempotent calls, and a frozen three-way effect class defaulting to the safe value | `state/invocation_journal.py`, `runtime/loop.py`, `tests/unit/test_invocation_journal.py`, `docs/guides/M10A_INVOCATION_JOURNAL.md` |

- Two claims are proven by determinism tests, not by argument: a completed side effect is not executed twice when the checkpoint that would have recorded it was lost (Test A), and a non-idempotent call found at `started` is never replayed (Test B). Test B is split into B1 (no side effect yet — the deliberate false positive) and B2 (side effect occurred — the real duplicate prevented).
- **Recovery now has a real entry point** (M10-A.1): `RecoveryCoordinator` orchestrates a scan of mid-flight runs and delegates every safety decision to `resume_running`, and `forge recover <workspace> [--run-id ID]` is the working production command. Concurrency is handled by the existing locks (the journal's uniqueness on `(run_id, logical_call_id)` and the checkpoint's optimistic revision), so two processes starting at once cannot both recover one run; the loser reports `conflict`.
- **Service-restart discovery is now wired** (M10-A.2). A thin global `TaskRegistry` records `task_id → workspace` (relative to the configured root, canonicalised before persisting and re-validated on every read, so a tampered entry cannot direct recovery at an arbitrary directory). It holds **no** authoritative status — the checkpoint remains the only status authority. The API scans once during startup, before serving, and only when `recover_on_startup=True` (default off); scanning only at that moment is what avoids mistaking a live task for a crash without introducing a lease. Verified end to end: a real app boots, finds a leftover task through the registry, and refuses to replay its unconfirmable non-idempotent write while still serving.
- The coordinator targets **single-process leftover RUNNING runs** and does not claim multi-worker failover. heartbeat, distributed lease, leader election, and worker ownership are explicitly out of scope.
- `recovery_semantics_version` is now **1**. The delivery guarantee is **not** described as at-most-once: completed results are reused and uncertain non-idempotent side effects fail closed. The effect classes identify when replay could be safe, but startup recovery does not yet continue `claimed`, missing-row, read-only, or idempotent calls; those outcomes stop with `recovery_unsupported` rather than advancing the model.
- `tool_attempts` is deliberately **not** created; `tool_invocations.attempt_count` is not a physical execution log, and M10-B will add separate attempt records when retry is designed.

## Earlier design notes

- **[Historical — superseded: ADR 009 is now implemented via M10-A; see the invocation-journal table above. The "not implemented" wording below is kept only as a design-era record.]** **ADR 009 — Tool Invocation Journal and Recovery Semantics** froze: the journal as the source of truth for tool side effects (the checkpoint owns control flow, the trace is evidence only and must never be read as "the tool did not run"); a three-way `effect_class` (`read_only` / `idempotent` / `non_idempotent`, defaulting to the safe one); a `claimed → started → completed / failed / indeterminate` state machine whose `started` write is durably committed **before** the tool is called; a recovery decision matrix; a two-layer schema (`tool_invocations` for logical calls, `tool_attempts` for physical executions, the latter declared but unused until M10-B); and four fault-injection tests, of which the `started` crash window is the load-bearing one. It explicitly does **not** claim exactly-once delivery.
- `effect_class` is assigned conservatively and `write_file` is deliberately **not** declared `idempotent`: its optimistic `expected_sha256` check makes a repeat fail rather than duplicate, which is not idempotency under a key. Declaring it otherwise requires proof that the same key plus the same content produces no second effect.
- Recovery must reconstruct the *conversation effect* (append the recovered observation as a tool message), not merely avoid re-executing the tool.

## Frozen holdout evaluation (M8-C, final)

| Area | Evidence |
| --- | --- |
| The holdout set has now been run once, after the experiment identity was frozen from live code constants, with 5 samples per case per arm on both profiles | `reports/agent-eval-holdout-natural-omlx.json`, `reports/agent-eval-holdout-equal-omlx.json`, `reports/freeze-manifest.json`, `docs/archive/M8C_HOLDOUT_REPORT.md`, `docs/archive/M8C_FREEZE_RECORD.md` |

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
- **The holdout split has still not been run live**, so no live holdout result is authorized; lack of a live run alone does not establish an unseen set. Holdout is deliberately excluded from `make gate`.
- **Live runs are resource-intensive on this host.** An unbounded per-request output ceiling previously froze a 24 GB machine; `live_eval_config` now bounds it and `--limit` marks a truncated run `complete_split: false`, which forces `measurement_valid: false`. A truncated run must never be cited as a measurement.
- M8-B surfaced and fixed four real harness defects, each with a regression test: loopback requests sent through the system proxy (502); over-strict planner response parsing that penalised Plan-Execute for Markdown formatting; a step-ceiling assertion that made `max_steps=1` no-tool cases structurally unpassable for Plan-Execute; and a wiring contract that reported a bad live plan as a broken harness rather than as a model-quality failure. Live profiles now exclude scorer probes and `budget_boundary` cases, which are unsatisfiable outside the keyless profile.
- **Historical #3 status:** automatic retry/backoff had not been implemented at that milestone; current invocation recovery and TaskRunner now apply effect-aware bounded retry decisions. #3 deliberately stops at classification plus structured feedback; `ToolErrorCode` and `recoverable` are its prerequisites, not a retry policy.
- Tool-failure observation form is an experimental variable: a controlled test showed the same case/model/budget running 4 steps with the pre-#3 raw string versus 11 steps with structured feedback (the model kept probing non-existent paths). The structured form is correct — it is what let a live model change tools — but it changes behaviour, so cross-version comparisons of affected cases must hold the observation form fixed or state the version.
- Bad Case capture exists (M9-B1) and one replay channel is implemented (M9-B3): a failing sample is persisted with its hash-chained trace, and `RecordedModel` replays a frozen violation deterministically. Three Bad Cases are captured, one per layer: a protocol violation (model returned multiple tool calls), two planner failures (a planner that answered with XML only, and one whose JSON had a stray character), and an execution-loop case that looks like a no-progress loop but is evidence-classified as model quality because its repeated searches used different queries. Remaining observability gaps are recorded and deliberately NOT fixed yet: a recurring tool error surfaced as a raw exception string rather than an actionable observation, and a rejected plan ending the run instead of returning the planner to the model. The `usage.steps=0` gap for a rejected response is closed on the recoverable path (the rejected inference now charges a step); on the non-recoverable path the difference is intended, since no further work is done. One contributing case-design weakness is also recorded (a fixture referencing a directory it does not provide).

- The keyless Agent Benchmark (`forge eval-agent --split dev`) validates the grading contract, metric pipeline, both runtime wirings, and budget interaction only. It does **not** compare ReAct against Plan-Execute, because the scripted model replaces the decision under test; strategy claims require live-model runs. Its token figures are synthetic (`token_source: "synthetic"`, `cost_metrics_valid: false`) and never enter cost metrics. Benchmark cases and the 7 scorer probes are reported separately, never as one pass ratio. The dev/holdout split exists and the holdout floor is enforced, but the holdout set has not been used for a live comparison, so no holdout result is authorized; holdout is deliberately excluded from `make gate` to keep it uncontaminated. Trace step projection (M9-A) exists, but its `logical_call_id`→`attempt_id` mapping is currently one-to-one in that historical measurement; current retry attempts are tracked by the invocation journal.
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

- The session store is single-machine POSIX/SQLite. The per-session writer lock is a
  local file lock, not a distributed lease; two hosts sharing one state directory are
  not supported. Authorization is a host callback, not a built-in IAM, and the HTTP
  adapter is not safe to expose without a real authentication front end.
- Sessions keep no automatic migration from the legacy knowledge tables, and
  conversation text never becomes reviewed long-term memory by itself. Deleting a
  session covers the session's own copies and its runs' local evidence; host-side
  backups, external attachments and domain retention remain the host's responsibility.
- Session acceptance is proven with synthetic applications and a deterministic model.
  Multi-turn quality, citation quality, tenant isolation in a deployed service and
  real legal/commerce outcomes are untested here.
- Path confinement and approval are defense in depth, not an OS sandbox. Isolation of
  executed repository code comes from `ContainerBackend`, which is opt-in and only as
  strong as the runtime that honours the flags (ADR 044); an application that composes
  `LocalBackend` keeps the old blast radius.
- The todo board is a plan record, not a correctness proof. It makes an abandoned
  objective visible and refuses a final answer while items are open; it cannot tell
  whether the items describe the right work (ADR 042).
- Prompt caching is a plan plus send-time markers. Savings are claimed only where the
  provider reports a hit (`model.cache.observed`); no cost reduction is claimed for a
  gateway that silently ignores `cache_control` (ADR 043).
- The skill install loop proves a package parses, trims, installs and passes its own
  declared smoke command. It cannot prove the instructions are good, and a permissive
  smoke command passes as easily as a rigorous one (ADR 045).
- Legacy knowledge HTTP approval capability is process-local; SDK grants are durable in SQLite. The limitation applies to the legacy HTTP profile, not the current SDK approval ledger.
- Two API processes demonstrate local process failover only. They share the laptop, filesystem, Docker runtime, and OMLX failure domains and therefore are not production high availability.
- The deterministic RAG dataset is a regression/control set, not a representative enterprise corpus.
- The benchmark measures local FTS5 + in-memory vectors, not Qdrant network performance.
- Image chunks are model-derived observations linked to the source image, not OCR ground truth.
- Memory deletion is an audit-preserving tombstone plus index removal, not secure erasure of the original audit record.
- Host/Origin guards protect the local browser boundary, but that legacy profile has no identity/tenant authorization; the new SDK adapters require a current host identity callback. Do not expose this profile to a network or public tunnel.
- The worker drill verifies queued work during an outage, not a SIGKILL in the middle of an external write. Automatic lease/outbox recovery is not claimed.
- A hosted lease is a liveness claim, not a wall-clock claim: renewal extends the
  deadline only to "last recorded progress + one ttl", and a worker that stops
  progressing releases the claim instead of renewing. Recovery therefore depends on
  every backend entry point touching `last_progress`; a new long-running operation that
  forgets to touch it can lose the lease mid-flight (it then fails closed with
  `StaleLease` rather than writing under another worker's epoch).
- The hosted PostgreSQL benchmark passes on this host (121 tests, 0 failures). Its two
  earlier defects are fixed rather than waived: the lease above, and a migration
  assertion that compared whole rows even though the migration adds
  `fh_cloud.invocations.authorization_mode`. That assertion now compares the surviving
  columns verbatim and checks the added column separately (`'approval'`, the only
  reading compatible with a legacy approved row that carries no governing grant).
- No SWE-bench result, production SLO, multi-agent superiority, cloud HA, or cost claim is authorized.

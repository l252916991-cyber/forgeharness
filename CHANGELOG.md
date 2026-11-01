# Changelog

All notable changes to ForgeHarness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/). The authoritative implemented/planned boundary
remains `docs/STATUS.md`.

## 0.12.0 engineering candidate (qualification recorded separately; not published)

- Add an optional async execution boundary to the existing serial runtime; keep
  local stores separate, single-use send closures and fail-closed suspension.
- Stage bounded compatible trace envelopes, with exact committed-prefix handling.
- Add explicit schema2 migration and complete PG initial input, checkpoint and
  evidence transactions; preserve historical governance facts and reject adoption.
- Add a concrete PG backend and trusted hosted serial service using the existing
  AgentRuntime, exact independent approval, full result verification/settlement,
  scoped context, epoch fencing and progress-based leases. Unknown results retain
  budget and are resolved without dispatching again. Deterministic rejections
  have separate logical usage facts. Qualification is recorded in the candidate report; the qualified
  0.11.0 artifacts retain their original identity.
- Add a revisioned todo board (`write_todos`, idempotent whole-board replacement) and
  gate the final action on it: a run with open items is refused and told which items are
  open, and a model that will not close its board fails with `unfinished_todos` instead
  of looping to the step cap. The board is derived from the transcript, not
  checkpointed, so it cannot disagree with what the model was told.
- Pin the task objective across compaction: `ContextAssembler(pin_task=True)` keeps the
  first user message verbatim and `pinned` state re-reads the rendered board on every
  request, so the one message whose absence is unrecoverable never enters the dropped
  set. Dropped history now spills to `<workspace>/.forgeharness/context/` and the
  summary carries `full: ctx://...`; `read_compacted_context` pages the originals back.
- Add prompt-cache planning (`CacheMode` off/prefix/explicit) that freezes markers
  in a validated exact wire snapshot before admission, and traces both the plan and
  the provider-reported hit. `OpenAICompatible` reports cached input tokens; the pinned
  board and the name-sorted tool catalogue keep the prefix stable.
- Add a `FileBackend` protocol with local, container and composite implementations.
  `ContainerBackend` runs commands with no network, a read-only root, a dropped
  capability set, `no-new-privileges`, a non-root user, equal memory and swap limits, and
  argv-only invocation; timed-out containers are discarded rather than reused. Coding
  tools take a backend factory and record which backend served each call.
- Add a skill install loop: fetch within size/scheme/host limits, validate with the
  strict loader, trim unavailable tools (recorded, not silently dropped), smoke-test in a
  explicitly configured backend, then promote under a per-package writer lock with
  upgrade backup and rollback -- refusing a same-name package with different bytes
  unless upgrading. A machine/process crash between upgrade renames can require manual
  backup restoration. `SubAgentSpec.skill_instructions` appends skill text after the
  operator brief.
- Extend the keyless Agent Benchmark with `write_todos` coverage and an `error_contains`
  assertion; two dev long-horizon cases prove the gate refuses an open board and that an
  unclosed board ends `failed`. The holdout case set is unchanged.
- Fix both hosted-PostgreSQL benchmark defects. A lease is now a liveness claim: renewal
  extends the deadline only to "last recorded progress + one ttl" and a worker that stops
  progressing calls the new `PostgresRunStore.release` instead of renewing, so a paused
  worker no longer blocks an independent takeover (its later writes still fail on the
  epoch fence). The migration test now compares the surviving columns of each row
  verbatim and checks the added `authorization_mode` column separately, which is what an
  additive migration can actually preserve. Final candidate counts are recorded
  in the new qualification report, separately from historical 0.11.0 evidence.
- Add packaging metadata, an `all` extra, a qualification-gated publishing workflow,
  and a ten-line [Quickstart](docs/QUICKSTART.md). This is not a publication claim.
- Observation compression now spills: the original is written to
  `<workspace>/.forgeharness/observations/` before the projection replaces it, the
  projection carries an `obs://` reference, and the new `read_observation` tool pages
  the original back instead of forcing a tool re-run.
- Tool policy is argument-aware: path boundaries, secret-path denials, command
  patterns, and sliding-window rate ceilings, each decision traceable to the rule that
  produced it. `CodingToolPolicy` now denies `.env`, keys, and `.git` outright.
- `forgeharness.__version__` is read from the installed distribution instead of a
  hand-edited constant; `CodingAgent`'s test command and approval ledger default so
  the zero-config path runs.
- Harden spill storage against symlink/hardlink and temporary-file substitution,
  bound stored/read bytes, and verify existing content before reusing it.
- Stage release bytes only after full source/clean-copy qualification, manifest
  and artifact digest checks; do not rebuild a different package for publication.
- Keep reference applications on the public SDK, including safe streamed file
  iteration; explicitly include the hosted consumer example in the sdist contract.
- Raise optional PDF/LangGraph security floors and patch the isolated comparison
  decoder dependency. Preserve the failed first candidate and declare a new
  comparison protocol before rerunning unchanged scenarios.
- Use no-follow directory descriptors for local file tools, search and repository
  mapping. Parent-directory substitution cannot redirect their reads or writes to
  an outside symlink target. Local command execution still requires an explicit
  container boundary for untrusted repository code.

## [0.11.0] - 2026-10-29 (engineering candidate)

- Add optional PostgreSQL operation-level run acceptance, tenant authorization,
  exact approval, atomic reservation, epoch-fenced ownership and receipt settlement.
- Preserve unknown and actual overspent usage across cancellation/revocation and
  takeover; allow late verified facts without letting stale workers advance state.
- Separate administrator provisioning from runtime, reject privileged role
  memberships, enforce tenant RLS and immutable receipt privileges.
- Require real PostgreSQL 18.6 contracts, combined coverage, clean-copy and separate
  optional-wheel consumer checks; retain base SDK dependency isolation.
- Keep Cloud state separate from existing SQLite execution. Harness integration,
  provider capacity, generic platform identity/API/UI and commercial SaaS remain planned.

## [0.10.0] - 2026-10-28 (engineering candidate)

- Add a separate invitation-only commerce workbench with account-backed login,
  CSRF, independent approval, current runtime authorization and private offline
  administrator bootstrap. Keep deterministic business fixtures explicit.
- Revalidate current member-management permissions inside write transactions;
  scope disabling and credential revocation to the managed workspace.
- Share atomic password failure accounting between login and approval, resolve
  local FastAPI dependencies, and clear the browser cookie on logout.
- Verify the workbench in a separate wheel consumer with optional Web extras;
  keep the base SDK installation independent of those extras.
- Reject reverse state adoption by the public-role demo, fence late browser
  responses across identity/session changes, and test the shipped page script.
- Refresh optional msgpack 1.2.2 to 1.2.4 after the audit identified
  CVE-2026-114613; preserve the rejected candidate without adding a waiver.
- Record SaaS state transaction contracts and preserve the 0.9.2 candidate.
  PostgreSQL runtime, cross-node execution and commercial SaaS remain planned.

## [0.9.2] - 2026-10-27 (engineering candidate)

- Add an approved, idempotent general commerce handoff for explicit customer
  requests with no selected product; keep tenant support access and application
  identity deterministic under commerce-consultation application/policy v2.
- Replace the consultation console's unrelated background-task panel with durable
  turn inspection and hash-chain events; add live/readiness probes.
- Add offline, manifest-bound SQLite/file backup and same-path restore operations,
  with an application-lifetime lock, owner-only restored directories and an
  end-to-end pending-approval recovery regression.
- Preserve SDK 0.9.1 candidate evidence. The 0.9.2 engineering qualification does
  not establish real merchant identity/data/ticket integration or business quality.

## [0.9.1] - 2026-10-27 (engineering candidate)

- Bind model invocation/capacity identities to the state store, complete run
  identity and node. Separate stores using identical public task IDs cannot
  release one another's unresolved capacity (PE-012, ADR 036).
- Reuse the persisted invocation ID during resume and pass that exact key to
  model receipt reconciliation; retain existing tool journal keys.
- Preserve 0.9.0 candidates and framework protocols. Reject their run identities
  under the new framework version, add an SDK 0.9.1 comparison declaration and
  rerun a new candidate independently after the post-gate defect reproduction.

## [0.9.0] - 2026-10-27 (superseded engineering candidate after PE-012)

- Add explicit bounded final-only model concurrency (1/2), single-writer R2
  settlement, independent invocation tickets and prepared sending boundaries.
- Add a host-owned provider dispatcher with shared cross-run capacity, bounded
  round-robin queues, a durable unresolved-capacity ledger and a POSIX owner lock.
- Preserve budget reservations through unknown calls and owner death; retain
  incurred usage after revoke/cancel and revalidate scope at provider entry.
- Keep tools exclusive in each run; add typed execution views, node timing facts,
  new-process fault tests and a keyless parallel/recovery demonstration.
- Use R2 orchestration schema 3 / recovery 5; old runs retain their original wheel.
  Add strict optional provider usage validation, no new runtime dependencies.
- Add a separately sealed same-topology concurrency experiment. Engineering and
  target-backend performance qualifications are recorded independently in
  `reports/parallel-execution-20261027/REPORT.md`; no general business/quality claim.
- Preserve the old framework comparison declaration; use a new SDK 0.9.0 protocol
  file with unchanged cases/criteria, and update the public recovery demonstration
  to assert recovery 5 explicitly. Add executable regressions for both declarations.

## [0.8.6] - 2026-10-26 (engineering candidate)

- Add opt-in fixed specialist collaboration: registered versioned experts,
  independently scoped evidence assignments, serial R2 decisions, a dependent
  coordinator, typed delegation inspection and source-identity verification.
- Add bounded local JSON output schemas to R2 model nodes. Reject ambiguous JSON,
  external schema references, wrong task IDs and out-of-scope citations before
  downstream execution, including trusted reconciliation; retain actual usage.
- Add deterministic recovery, authorization, budget and process-death regressions
  and a three-process synthetic lost-response/reconciliation example. Include it
  in sdist and minimal-wheel release gates; add no runtime dependencies.
- Qualify in `reports/multi-agent-20261026/`; preserve historical candidates and
  host wheel bindings. This mode has no parallel, autonomous tool-loop, isolated
  write or real-model quality claim. Existing R2 runs keep their original wheel.

## [0.8.5] - 2026-10-26 (engineering candidate)

- Add `RunKernel.call_model(include_tools=False)` for final-text-only phases.
  Omit schemas before canonical request preparation, keep the registry and tool
  permissions intact, and reject non-boolean flags before provider invocation.
- Correct fixed-flow and three-role benchmark wiring to use that option. Retain
  the interrupted 27B pilot as instrumentation diagnostics, then repeat development
  and freeze a fresh protocol before formal sampling. Tasks and grading stay fixed.
- Add regressions for provider payloads, trace fidelity, concurrent calls, shared
  accounting, unchanged read-only authorization and all benchmark final-only phases.
- Freeze and verify candidate-r3 independently. Earlier candidate-r2 and host wheel
  bindings remain historical evidence; existing R2 runs retain their exact version.

## [0.8.4] - 2026-10-26 (engineering candidate)

- Add a current capability/host binding index, executable read-only quickstart,
  recovery demonstration and interview diagrams with explicit residual limits.
- Add a three-arm synthetic diagnosis experiment using the existing Harness and
  kernel, external grading, task-cluster intervals, complete failure denominators,
  local model readiness and source/model identity binding. Formal sampling remains
  pending; local OMLX returned HTTP 507 and cannot establish strategy quality.
- Repeat six existing R2 fault classes in independent processes; add timeout/restart
  regression and exact-wheel host checks, including real Commerce process death.
- Refresh optional Brotli/setuptools and the independent Pydantic comparison lock
  for newly reported advisories; reseal the version-declared comparison protocol.
  No new dependency waiver. Host vendor bindings remain unchanged.
- Keep virtual-environment interpreter symlinks intact in the new host driver;
  retain all earlier failed attempts and add a regression.
- SDK API and state schemas stay unchanged. Existing R2 identities bind framework
  versions: keep old runs on their bound wheel; no implicit version takeover.

## [0.8.3] - 2026-10-23

- Preserve an exact zero nested-step allowance for tool nodes via the shared
  executor's node step limit, while keeping RunBudget's positive run-step invariant.
- Verify tools that obey zero/positive nested allowances can complete without
  ungranted work. Retain the 0.8.2-r1 gates and later R2-006 reproduction separately.

## [0.8.2] - 2026-10-23 (superseded after post-gate R2-006)

- Revalidate the complete frozen run identity during active admission and settlement,
  preventing nested argument/resource maps or budgets from changing after start.
  Preserve already committed journal results when that guard refuses continuation.
- Add active mutation and safe reconstruction regressions. Retain the 0.8.1-r1
  automated 92/92 report and its later defect reproduction as separate evidence.

## [0.8.1] - 2026-10-23 (superseded after post-gate R2-005)

- Synchronize release-verification example fixtures and the exact sdist allowlist
  assertion with the new three-process R2 demo. Keep all validation assertions.
- Retain the unqualified 0.8.0-r1 report and artifacts; qualify a new immutable
  0.8.1-r1 candidate independently.

## [0.8.0] - 2026-10-23 (first candidate; unqualified)

- Add explicit durable graphs with stable nodes, approval collections and atomic
  consumption/admission, budget reservations, deterministic joins and scoped
  recovery/reconciliation. Arbitrary one-shot kernels keep their existing limits.
- Preserve unknown model costs and tool effects, account physical attempt/nested
  usage in the shared journal, fence local owners, persist cancellation/deadline
  facts, and publish deduplicable transactional outbox events.
- Write checkpoint v2 / recovery v4 only for new R2 runs, preventing old ReAct
  code from interpreting graph progress as a transcript. Existing states remain v1.
- Add deterministic process-death fault tests and a three-process minimal-wheel
  example. Qualify in a new candidate directory without altering historical reports.

## [0.7.1] - 2026-10-23

- Permit tool usage exactly at a token limit, including zero-token tool-only
  workflows. Future calls still obey admission checks and overspending fails.
  The regression was reproduced on the 0.7.0 candidate before the fix.
- Qualify this patch independently; retain the 0.7.0 artifacts and reports.

## [0.7.0] - 2026-10-23

- Extract shared policy, invocation journal, retry, recovery and allowance mechanics
  into `ToolExecutor`; ReAct and the read-only kernel reuse the same implementation.
- Serialize kernel model/tool admission and settlement; account nested steps and
  calls identically to ReAct. Reject tools whose READ risk hides a non-read effect.
- Custom write, approval and resume remain unavailable pending R2. This candidate
  requires fresh source/clean-copy qualification; old 0.6.0 reports do not apply.
- Preserve the first candidate's failed dependency audit, then update optional
  pypdf 6.19.0 to 6.21.2 within the existing range and requalify separately.
  Comparison protocol changes only the Forge distribution label and its seal;
  scenarios, grading and decision rules remain unchanged.

## [0.6.0] - 2026-10-21

- Bump the distribution version to 0.6.0 (`pyproject.toml`, `forgeharness.__version__`
  and the verification driver) to distinguish this release from the two different
  artifacts that both carried the 0.5.0 label on 2026-10-21: the commerce-bound
  `58579893…` wheel qualified under `reports/request-preflight-2026-10-17/` (unaffected
  by this change) and the `8275baba…` wheel built by the `reports/framework-next`
  verification run. The declared forge arm of the comparison protocol is re-declared
  as 0.6.0 and re-sealed (`evals/comparison/protocol.sha256`); no scenario, grading
  rule or decision rule changed.
- Fix the six pip-audit gate failures of the 2026-10-21 framework-next verification
  run by upgrading locked optional/contrast-environment dependencies:
  aiohttp 3.14.3 → 3.14.5 (CVE-2026-111063/111064/111065/111066/111748/111749/111750,
  GHSA-2g87-pp6c-29x5), certifi 2026.7.22 → 2026.10.19 (PYSEC-2026-4320) and
  pillow 12.3.0 → 13.0.0 (CVE-2026-112395/112397/112400/112404/112895/112896/112897/112899;
  the `knowledge`/`cli` extra bound moves to `>=13,<14`), plus the transitive
  yarl 1.24.5 → 1.25.1. The two independent comparison-environment lockfiles
  (`evals/comparison/environments/deepagents`/`pydantic`) re-lock
  certifi 2026.10.19 as well; their declared engine versions are unchanged.
  The existing NLTK exception (PYSEC-2026-3740, FH-019) is
  unchanged; no new waiver is added.
- Harden the current release entry: accept a workspace-reports-scoped candidate
  directory and a freeze-only mode, preserve existing manifests on source drift,
  and align CI with the new framework candidate. Reconcile whole-tree formatting,
  SDK/version and replay documentation, and distinguish stale pytest nodeids from
  a freshly collected test count. See `docs/BASELINE_VERIFICATION.md`.
- Add `Capability` and `compose_application` to compose applications from ordered,
  trusted contributions of tools, instructions and context providers. Policy, verifier,
  budget and authorization remain explicit application inputs; duplicate capability/tool
  names fail. Existing `ApplicationSpec` construction is unchanged. See ADR 022.
- Add `Orchestrator` and `RunKernel` so an application can supply its own control flow
  while model calls, read-only tool calls, budget, trace, checkpoints and verifier
  execution stay governed. Custom runs are one-shot and reject write/approval tools;
  ReAct remains the default and keeps approval/recovery semantics. See ADR 023.
- Add an executable core/application boundary test and migrate the legal case-QA app to
  `Capability` composition. Two API-v1 Coding compatibility shims are explicitly frozen
  for removal at API v2. See ADR 024.
- Add a read-only LexVault-shaped case bundle, bundle adapter, parallel specialist DAG
  orchestrator and `[page:N]` citation verifier. It is synthetic/bundle-only: no live
  database, no writes, and no semantic/legal-quality claim. See ADR 025.
- Add a read-only LexVault SQLite exporter that preserves document/page identity while
  assigning bundle-global `[page:N]` numbers, plus a four-question read-only DAG test
  that verifies the source database hash is unchanged. A one-off run on local case 1
  (14 pages/5 documents) completed four questions with a scripted model and an unchanged
  hash; this is not a real-model or legal-quality claim.
- Migrate the commerce reference applications to `Capability` + `compose_application`;
  tool order, policies and verifiers are unchanged.
- Add ADR 026 documenting why custom-orchestrator write tools stay on ReAct for API v1,
  with fail-closed tests for write tools and approval-required calls.
- Add application-level conformance/take-apart tests for the legal and commerce reference
  apps (identity, unique executable tools, stable order, policy/verifier presence,
  wrong-binding rejection) and publish the current gaps in `docs/CONFORMANCE.md`.
- Add a shared runtime conformance matrix for legal DAG and commerce: successful
  run/trace/inspect, verifier rejection, wrong application binding and missing-resource
  fail-closed behavior.
- Harden the LexVault read-only path (ADR 027): per-page `text_sha256` with
  mismatch-on-supply rejection, content-addressed `CaseBundle.snapshot_digest`,
  `[page:N]「exact quote」` verification with optional strict mode and digest
  recomputation; the exporter now reads in one read transaction, preserves raw page
  text and binds the default version to page digests. The import-boundary scan also
  detects `import_module("...")` literals, which surfaced a third frozen Coding shim
  (`context/repository.py`); the allowlist is corrected to three files.
- Record how an application was composed (ADR 028): `ApplicationComposition`
  (ordered capability names + structural digest) on `ApplicationSpec`, and an
  `application.composition` trace event for every composed run. Migrate the
  consultation, records and coding reference apps to `compose_application`, so all
  reference apps now share one composition API. Reposition the package description
  and README SDK list around the application-neutral framework instead of the
  coding vertical.
- Unify model invocation (ADR 029): `invoke_model_once` is the single governed
  model call shared by `AgentRuntime` and `RunKernel`, so preparation, trace
  events and token accounting cannot drift. Custom orchestrators now charge
  rejected tokens for recoverable protocol violations (matching ReAct) and raise
  with the stable violation code. Split `sdk/application.py` into
  `sdk/spec.py`/`sdk/kernel.py`/`sdk/harness.py`; the old module is a re-export
  shim until API v2, and the private bridges are renamed
  `ModelBridge/ToolBridge/TraceBridge`.

## [0.5.0 synthetic candidate] - 2026-10-15

- Add scoped staging/validation/import tasks, local version-bound retrieval, durable
  task states, cancellation/reconciliation and optional authenticated task controls.
- Add synthetic legal page QA and pending-review notes, and commerce comparison,
  follow-up, timestamped price/stock, approved idempotent handoff and local console.
- Revalidate durable tool-read sources for session reads and recovery; make failed
  preparation cleanup retryable; batch iterative history checks with a 512-turn bound.
- Add 40 legal/80 commerce development contracts, a distinct sealed synthetic fixture
  and consume-before-decode governance. Keyless reports never qualify business quality.
- Add fresh-copy release verification, public-data packaging and minimal-install
  demonstrations to CI. See BUSINESS_PILOTS for residual and external prerequisites.

## [0.4.0 candidate] - 2026-10-14

- Add durable scoped sessions over the existing Harness and WorkflowRunner:
  conversations fixed to an application, policy version, tenant, owner, resources
  and workspace; ordered turns with stable request keys; bounded untrusted history;
  a per-session single writer; crash reconciliation that never reports an
  unconfirmed turn as success; deletion tombstones that retire an identity; and
  delegated approval with a separately authenticated, single-use grant. Export the
  session types from `forgeharness.sdk`; document the contract in `docs/SESSIONS.md`.
- Add an optional session HTTP router (`session_router`), which imports FastAPI
  itself so the base SDK and wheel stay web-free, and a synthetic legal/commerce
  client (`examples/session_demo.py`) covering restart, replay, revocation,
  cross-tenant refusal and delegated approval. Keep SDK API v1 and the legacy
  knowledge interfaces; migrate no legacy sessions.
- Add the S1 qualification gate (`benchmarks/session_verification.py`): candidate
  freeze that refuses to overwrite a differing manifest, fresh-copy re-verification
  in a clean Python 3.12 environment, wheel/sdist membership bound to the frozen
  source, and a minimal wheel-only install that must import without Web extras.
  Qualification is bound to `reports/session-release-audit.json`; S2–S5 and real
  business/model quality remain future work and FH-015–FH-018 stay open.
- Fix three defects found while verifying S1: a tombstoned session identity was
  reported as a naming conflict (409) instead of unavailable (403); replaying a
  consumed approval grant on a finished turn returned a silent success instead of
  rejecting it; and a workflow turn whose host adapter raised kept looking unstarted
  instead of recording the durable workflow state and its error.
- Fix three gate and packaging defects: the verification driver discarded its
  failure evidence when the source tree could no longer be hashed; the gate invoked
  Ruff with `--exclude reports`, which replaces Ruff's default excludes and linted
  the whole virtual environment; and the sdist allowlist rejected `.gitignore`,
  which the packaging tool adds by default, so `.gitignore` now belongs to the
  frozen content identity rather than being exempted. Make the architecture test
  exempt only declared optional adapters and assert that `import forgeharness` pulls
  no optional platform package. Import the offline drivers under `benchmarks/` as a
  package.

## [0.3.0] - 2026-10-12

- Add public durable sequential DAG composition over Harness, static whole-flow
  resource reservations, stable child recovery, workflow verification and deletion.
- Add scoped immutable content revisions with CAS/idempotent draft creation,
  separate human review/publication, source dependency invalidation and expiry.
- Add exact-span citations, published context/tools and verifier composition.
- Add synthetic legal note and commerce consultation/content reference workflows;
  retain existing SDK v1, Coding and commerce compatibility with no new dependencies.
- Clarify historical plan status and the implemented/planned boundary in BUSINESS_MODULES.
  Qualification is tied to `reports/modular-release-audit.json`, not this changelog.

## [Unreleased historical runtime entries]

### Added

- A freeze manifest for final evaluations (`forge freeze-manifest`): records the
  revision, model parameters, and the observation/recovery/benchmark/budget versions
  plus the holdout id hash and sample count, all read from live constants so it
  cannot drift from the code. Reports now carry `holdout_case_ids_hash`, tying a
  figure to the exact evaluation set rather than only to the file bytes.
- Version generations are now distinguished: `recovery_semantics_version` 1 = journal
  only (M10-A), 2 = journal + retry/attempts + soft deadline; `budget_profile_version`
  1 = the never-binding hand-written cap of 12, 2 = the rule-derived cap of 4.
- Whole-run wall-clock deadline (M10-C): `RunBudget.max_wall_seconds`, checked
  between steps and reported as a failure with `error_type=deadline_exceeded`. It is
  deliberately not implemented by cancelling an in-flight tool, because aborting a
  side effect mid-write would produce the ambiguous state ADR 009 exists to prevent;
  the ceiling stops new work instead. The clock is per invocation, so a resumed run
  gets its own allowance, and both `clock_fn` and `sleep_fn` are injectable so tests
  never wait in real time.
- Tool retry with attempt records and graded timeouts (M10-B): a pure
  `decide_retry(effect_class, error_code, attempt_no, max_attempts)` owns the single
  retry rule, a `tool_attempts` table records physical executions separately from
  logical invocations, backoff is deterministic (100/200/400ms capped at 1s, no
  jitter), and `ToolSpec.timeout_seconds` lets a tool own its ceiling.
- Service-restart task discovery (M10-A.2): a thin global `TaskRegistry` records
  `task_id → workspace` so a restarted service can find a leftover run's durable
  state. It is a locator only: it stores no authoritative status, persists the
  workspace relative to the configured root, and re-validates it on every read, so
  a tampered entry cannot point recovery at an arbitrary directory. The API scans
  once during startup (opt-in, `recover_on_startup`, default off) before serving
  requests, which avoids mistaking a live task for a crash without a lease.
- Recovery coordinator and a real recovery entry point (M10-A.1): a thin
  `RecoveryCoordinator` enumerates mid-flight runs, checks the semantics
  generation, delegates to `resume_running`, and isolates a failing run so the scan
  continues. It holds no safety logic of its own — the journal's recovery matrix is
  the single authority. `forge recover <workspace> [--run-id ID]` is the working
  production command, and `CodingAgent.recover` exposes it to compositions.
- Tool Invocation Journal and effect-class recovery (M10-A, ADR 009): a durable
  record per logical tool call as the **source of truth for side effects**, a
  `claimed → started → completed / failed / indeterminate` state machine whose
  `started` write is committed before the tool is called, a frozen three-way
  `ToolEffectClass` on `ToolSpec` defaulting to `non_idempotent`, and a recovery
  entry point that reuses a completed result or refuses to replay an unconfirmable
  non-idempotent call. Verified by crash-injection tests, including the `started`
  window in both physical forms (B1 deliberate false positive, B2 real duplicate
  prevented). `recovery_semantics_version` is now 1.
- ADR 009 (Tool Invocation Journal and Recovery Semantics): a durable journal as the source of truth for tool side
  effects, a three-way tool effect class defaulting to the safe value, a
  `claimed → started → completed / failed / indeterminate` state machine whose
  `started` commit precedes any side effect, a recovery decision matrix, and four
  crash-injection tests including the `started` window. It does not claim
  exactly-once delivery.
- Frozen calibration and sampling rules (M8-C-prep): the equal-resource step cap is
  derived by a pre-registered rule rather than hand-picked — pooled dev-natural P75
  of `total_run_steps`, applied identically to both arms, and only usable when at
  least 10% of runs actually bind (`constraint_effective`). Holdout sampling is
  fixed at 5 runs per case per arm with per-case counts and dispersion reported,
  not a single aggregate ratio.
- Reports now pin the experiment identity: `model_parameters` plus
  `observation_contract_version`, `recovery_semantics_version`,
  `benchmark_version`, and `budget_profile_version`. Runtime behaviour is itself an
  experimental variable — structured failure feedback alone changed a case from 4
  steps to 11 — so these fields make a cross-version comparison visibly
  cross-version.
- Structured tool-failure observations (M9-B3 #3): tool exceptions are classified
  into a frozen four-code taxonomy (`path_not_found`, `invalid_arguments`,
  `timeout`, `tool_execution_error`) and rendered as a deterministic JSON
  observation carrying `code`, `recoverable`, the model's own argument values, and
  a suggested next step. `ToolOutput.error` carries the structured failure, and the
  trace keeps `error_code` plus `exception_class`.
- Bounded planner protocol recovery (M9-B3 #2): a planner response that breaks the
  plan protocol earns up to `RunBudget.max_planner_retries` correction attempts
  (default 1) before the run fails with `repeated_planner_protocol_violation`.
  Only protocol failures retry; a well-formed but poor plan does not. The
  correction message states the requirement and never forwards raw parser text.
- `PlanProtocolViolation` carries a stable `code` (`missing_plan_json`,
  `invalid_plan_json`, `plan_schema_violation`, `planner_called_tool`) so a
  planner violation is classifiable without matching a message. The parser's
  acceptance criteria are unchanged: malformed JSON is still rejected.
- The planner's rejected response is recorded on `plan.failed`, and
  `planner_replay_script` rebuilds the model-side outcome from it. Without the raw
  response a replay could reproduce the failure but never exercise a fix for it.
- Deterministic Bad Case replay and the first red→fix→green loop (M9-B3):
  `evaluation/replay.py` provides a minimal `RecordedModel` that replays a frozen
  model outcome — including a recorded protocol violation — so a harness fix can
  be proven without depending on model randomness. It deliberately does not do
  trace editing, branching, or replay-from-checkpoint, and cannot show that a
  prompt change helped.
- `RunBudget.max_protocol_violations` bounds how many recoverable protocol
  violations may be fed back before a run fails with
  `repeated_protocol_violation`.
- Live Agent Benchmark profiles (M8-B of `docs/NEXT_PHASE_PLAN.md`):
  `forge eval-agent --profile equal|natural --model <name>` runs a real model
  through the same evaluator, projection and grading contract as the keyless
  profile. `equal` shares one whole-run budget across arms with no planner
  grant; `natural` gives both arms a generous budget so planner overhead appears
  in measured cost. Live profiles require repeated sampling and report
  `measurement_valid` independently of model quality.
- `--limit` runs the first N cases; such a run is reported with
  `complete_split: false` and can never be counted as a measurement.
- Bad Case capture and diagnosis (M9-B1 of `docs/NEXT_PHASE_PLAN.md`):
  `forgeharness.evaluation.bad_case` defines a `BadCase` record bound to its
  origin (`revision`/`profile`/`arm`/`model`/`sample_id`) and to its evidence
  (`trace_hash`, and the raw hash-chained trace persisted under
  `reports/bad-cases/`). A failing sample is captured automatically, and
  `--only-cases <ids>` reproduces a single failure cheaply.
- Artifact identity is `{profile}-{arm}-{case_id}-s{sample_id}`, because the same
  case can behave differently under `equal` and `natural` profiles and across
  repeated samples; omitting either component would silently overwrite evidence.
- Derived diagnostics for later verification: `repeated_call_signature`,
  `repeat_count`, and `observation_hashes`, plus `harness_gap`,
  `case_design_note`, `failure_class`, and `failure_point`.
- Failure classification is structural and driven by a stable `error_code` from
  the provider boundary, not by matching a message, so rewording does not change
  the verdict. `ModelProtocolError` now carries `code` and the rejected tool-call
  count.

### Changed

- Tool failures no longer send raw exception text to the model. Classification is by
  exception *type*, never by matching a message, and the failure's `details` are
  copied from the model's own arguments so a host path cannot leak into the model's
  context. An unrecognised exception falls back to `tool_execution_error` with
  `recoverable: false`, because a failure the harness cannot characterise is not one
  it can promise is retryable. A failed call still counts as a `logical_tool_call`
  and a `tool_attempt`.
- Planner attempts are charged in full (step and tokens) whether or not they
  succeed, so a plan recovered by retry stays visibly more expensive than one that
  worked first time: correction is not free.
- The `planner_lifecycle` contract check now asserts one to
  `1 + max_planner_retries` planner invocations instead of exactly one, matching
  the bounded-retry semantics.
- A recoverable model-protocol violation no longer terminates the run. When a
  response arrives intact but breaks the single-action protocol, the harness
  records `protocol.violation`, charges the step and its tokens, appends an
  actionable correction message, and asks the model again — while admitting and
  dispatching nothing and never choosing an action on the model's behalf.
  Non-recoverable provider errors keep failing terminally, unchanged.
- `ModelProtocolError` moved to the provider-neutral model protocol and now
  carries `recoverable` (derived from a shared code set, overridable) and `usage`,
  so a rejected request's tokens are no longer lost with the exception. The trace
  records `error_code`, `received_tool_calls`, `recoverable`, and the rejected
  usage on `model.failed`.
- No-progress is only claimed when the evidence supports it: identical tool name,
  identical canonicalised arguments, **and** identical observations. Repeating a
  call that returns different results is not no-progress. This prevented
  mislabelling `retr-find-then-read`, whose repeated searches use different
  queries.
- Trace events now record `error_code` and `received_tool_calls` on a model
  failure, and the projection exposes `model_requests`, so a request whose
  response was rejected is visible instead of disappearing.

### Fixed

- A recovered run could be misclassified as a harness failure. A `model.failed` or
  `plan.failed` event was assumed to be the cause of failure and the recorded run
  status was hard-coded as failed, so once protocol violations became retryable a
  *successful* run that had corrected a violation was labelled a planner or adapter
  defect — hiding its real (grading) reason and polluting the Bad Case set. The
  recorded status is now used, and a boundary event only counts as the cause when
  the run did not recover. Six mislabeled artifacts were removed.
- Scorer probes were captured as Bad Cases. A probe is *designed* to fail — it
  validates the evaluator, not the agent — so every run wrote 14 entries
  describing no real defect. Capture now skips probes, with a regression test.
- Bad Case artifacts were written under the repository root, so every test run
  polluted the working tree (100 stray files, untracked). The artifact root is now
  derived from the report's own output path, with a regression test.
- The recorded planner and executor replay scripts were identical, because the
  split filtered on a `phase` field that `model.action` events do not carry. The
  two scripts now come from their own sources — executor decisions from
  `model.action`, the planner's outcome from `plan.created` — so a Plan-Execute
  replay can no longer feed the plan into the executor loop.
- `PlanExecuteRuntime` rejected a planner response wrapped in a Markdown code
  fence, so a correct plan could fail on formatting alone and bias any runtime
  comparison. A fenced block is now unwrapped, while the plan itself is still
  strictly validated (empty or unterminated plans are still rejected).
- `OpenAICompatibleModel` sent loopback requests through the system HTTP proxy,
  which answered 502. A loopback base URL now disables environment delegation
  while remote endpoints keep it, matching the other local clients.
- Benchmark step ceilings were unfair to Plan-Execute: a case's `max_steps`
  describes the executor phase, but the planner decision was not offset, so
  `max_steps=1` no-tool cases were unsatisfiable for Plan-Execute regardless of
  behaviour. The planner decision is now offset in every profile, and
  equal-resource pressure is expressed through the shared budget instead.
- Live model configs are centralised in `live_eval_config` with a bounded
  per-request output ceiling and thinking disabled. An unset `max_tokens` made a
  local server reserve its full context per request; across hundreds of
  sequential inferences that can exhaust unified memory and freeze the host.
- The benchmark's runtime-wiring contract no longer conflates harness defects
  with model quality. The planner check now counts planner **invocations**
  (`planner_requests`) rather than valid plans (`plan_events`), and a planner
  that raises still consumes its step. Previously a live planner answering with
  prose marked the whole arm's contract as broken, which blamed the harness for a
  model defect and hid the real quality signal.
- Live profiles run quality cases only. Scorer probes are negative controls
  that validate the evaluator, and `budget_boundary` cases assert a specific
  `EXHAUSTED` outcome under a per-case budget that live profiles deliberately
  override; both are unsatisfiable or meaningless against a real model, so
  including them only diluted the comparison with always-failing cases.
- Trace step projection (M9-A of `docs/NEXT_PHASE_PLAN.md`):
  `forgeharness.observability.steps` turns a verified hash-chained trace into
  ordered step records, and `forge trace-steps <task-id>` renders them. It is the
  project's single trace→step projection — the agent evaluator now reads its step
  facts from the same code path, so diagnostics and benchmark verdicts cannot
  disagree.
- Step identity is frozen ahead of retry support: `logical_call_id` (one model
  tool-call decision, stable across retries), `attempt_id` (`<call_id>#<n>`), and
  `step_id` (display order only). The mapping is one-to-one today; M10 extends it.
  Model latency (`model_latency_ms_derived`) and tool latency (`tool_latency_ms`)
  are reported separately and never conflated, and `budget_snapshot` records the
  remainder after each step so an exhaustion is diagnosable directly.
- Trace events now carry `call_id` on policy, tool and approval events, a
  `budget` remainder snapshot, and a `phase` tag on `model.request`; the planner
  records its own request event and usage so planner cost is attributable.

### Changed

- An opened call with no completion projects as `incomplete`, and a call held for
  approval as `awaiting_approval`; neither is silently upgraded to success.
  `tool_sequence` reports the decision order (including calls a budget gate
  refused) while the new `executed_sequence` reports what actually ran.
- Keyless Agent Benchmark foundation (M8-A): `evals/agent_cases.json` holds 61
  frozen cases across 8 categories with an enforced dev/holdout split and 7
  negative scorer probes. A unified evaluator drives **both** the ReAct and
  Plan-Execute runtimes and checks their wiring contract (step accounting,
  budget-gate counting, planner lifecycle). Exposed as
  `forge eval-agent --split <dev|holdout>`.
- Benchmark cases and scorer probes are reported as separate fields, so a
  negative control can never be read as a failed task. Probes are excluded from
  every headline metric and exist only in the `dev` split.
- The keyless report sets `token_source: "synthetic"` and
  `cost_metrics_valid: false`; synthetic token figures are kept under
  `pipeline_diagnostics` and never promoted to cost metrics. `grading_agreement`
  is `null` on a probe-free split rather than a vacuous `1.0`, and `qualified`
  means "wiring correct" for keyless but only "measurement valid" for a
  live-model profile.
- `make gate` runs only the dev split of the Agent Benchmark. The holdout split
  is deliberately excluded so its per-case results cannot guide tuning, which
  would turn it into a second development set. Running holdout is an explicit
  act reserved for live-model comparison.
- Plan-Execute runs in the benchmark are granted one extra executor step for
  their planner, with the step assertion offset to match. Previously
  `max_steps=1` let ReAct execute a tool call while denying Plan-Execute any
  execution, which would have made the two arms incomparable.
- CI now runs `make gate` instead of `make check`: the release gate adds the
  keyless control evaluation, a CLI contract smoke test, lockfile consistency
  (`uv lock --check`), and a `pip-audit` vulnerability scan (PYSEC-2026-3740
  ignored as the documented accepted residual risk, see
  `docs/review/FINDINGS.json` FH-008).
- Added a structured JSON logging layer (`forgeharness.observability.logging`)
  writing one JSON object per line to stderr; the HTTP API logs every request
  with method/path/status/duration and blocks cross-origin mutations as
  warning-level events. Complements, does not replace, the hash-chained trace.

### Fixed

- Framework-comparison CLI commands (`forge langchain-rag`, `forge
  langchain-agent`, `forge framework-compare`) are now actually registered on
  the Typer application; the README previously documented commands that did not
  exist. A CLI contract test (`tests/unit/test_cli_contract.py`) prevents
  documentation from outrunning the CLI surface again.
- The LangChain RAG arm ran its retrieval stage twice per query and crashed on
  Pydantic field assignment (`HybridRetriever`); both defects are fixed and
  covered by unit tests.
- `pyproject.toml` declared `langchain>=0.3,<1` alongside
  `langchain-core>=1,<2`, which is unsatisfiable; the frameworks extra now pins
  the mutually compatible 1.x generation (langchain, langchain-core,
  langchain-openai, langgraph) and drops the unused `langchain-community`.
- The LangChain chat client now bypasses desktop proxies (`trust_env=False`),
  matching the native OMLX adapter, so loopback model calls no longer fail
  through a system proxy.

### Added

- `forge framework-compare` writes a revision-bound measured benchmark
  (`reports/framework-comparison.json`) of native retrieval versus the LangChain
  path over identical queries; missing optional dependencies are recorded as a
  skipped arm instead of estimated numbers.

### Removed

- Process/status summary documents duplicated across the repository root and
  `docs/` were consolidated; superseded material now lives under
  `docs/archive/`. `docs/STATUS.md` remains the single source of truth for the
  implemented/planned boundary.

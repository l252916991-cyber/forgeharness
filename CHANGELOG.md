# Changelog

All notable changes to ForgeHarness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/). The authoritative implemented/planned boundary
remains `docs/STATUS.md`.

## [Unreleased]

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

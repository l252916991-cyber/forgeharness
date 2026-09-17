# Evaluation and claim policy

## Reproducible suites

| Command | Purpose | Current checked-in evidence |
| --- | --- | --- |
| `forge eval-control` | Seven deterministic Harness control/failure cases | `reports/control-eval.json` |
| `forge eval-rag` | Fresh-index 60-case retrieval/citation/refusal gate | `reports/rag-eval.json` |
| `forge eval-reviewer` | Same-data baseline/Reviewer quality and latency enablement decision | `reports/reviewer-experiment.json` |
| `forge bench-retrieval` | 5,000 chunks, 10 warmups, 100 requests, concurrency 10 | `reports/retrieval-benchmark.json` |
| `forge qualify-omlx --quick` | Two real cases for each OMLX capability | `reports/omlx-qualification-quick.json` |
| `forge qualify-omlx` | 20 tool + 20 intent + 20 vision + 10 embedding + 30 reranker cases | `reports/omlx-qualification.json` |
| `python benchmarks/platform_verification.py` | Live two-API, Postgres, Redis/ARQ, Qdrant, Nginx recovery/failover drill | `reports/platform-verification.json` |
| `python benchmarks/freeze_candidate.py` | Exact tracked and unignored file hashes for an uncommitted candidate | `reports/candidate-manifest.json` |
| `python benchmarks/verify_clean_candidate.py` | Fresh locked Python 3.12 source-copy verification, without reusing `.venv` | `reports/clean-verification.json` |
| `python benchmarks/check_branch_coverage.py` | Pure branch gate, separate from statement/branch combined coverage | `reports/coverage.json` |

Every result report includes the visible Git revision and per-case or workload configuration. Because this candidate contains user-owned uncommitted changes, the final audit additionally verifies every tracked and unignored candidate file against `candidate-manifest.json`; result and review directories are excluded to avoid a circular hash.

## Gates

- Hybrid Recall@5 >= 0.85.
- Pure branch coverage >= 85%; `coverage.py` combined coverage is reported separately and cannot substitute for this gate.
- Citation precision >= 0.90.
- Unsupported-answer rate <= 0.10.
- Reranking MRR@10 must not fall below fused MRR@10.
- Deterministic warmed retrieval p95 < 500ms at the declared workload.
- Full OMLX tool-call and intent pass rate >= 0.90, vision >= 0.85, embedding >= 0.90 and reranker >= 0.80.
- Reviewer may default on only if answer quality improves by at least 0.05 and median latency is no more than 2x the single-agent path.

## Current measured results

- RAG control set: 60 cases, Recall@5 1.000, MRR@10 1.000, citation precision 1.000, unsupported-answer rate 0.000.
- Local deterministic benchmark: 5,000 chunks, 100 measured requests, concurrency 10; the latest report passed the <500ms retrieval p95 gate. Use the report's measured value rather than an older faster run.
- Full OMLX qualification: tool 20/20, intent 20/20, vision 20/20, embedding 10/10, reranker 30/30. The final full run's median latencies were about 2.49s, 0.91s, 42.97s, 0.21s, and 0.15s. A preceding long-running attempt was interrupted and OMLX restarted; cold/loaded-server behavior varies materially.
- Reviewer experiment: both arms 60/60 on the deterministic control set, 0 percentage-point improvement; Reviewer remains default-off. Use the latest report for latency; fake-model timing differences are not a speedup claim.
- Live platform drill: 30/30 queued jobs succeeded, the additional outage-time job recovered, and 20/20 requests succeeded after one API process stopped. Use `platform-verification.json` for the current readiness/upload percentiles.
- Locust's saved CSV snapshot records 1,821 requests and zero failures with 10 simulated users, configured 30-second duration, a single keyless API and an initially empty, growing fixture corpus. It is not a 5,000-chunk/Qdrant/OMLX throughput result. Console shutdown totals may exceed the final periodic CSV snapshot; use the saved artifact's denominator.
- Runtime/platform/development dependency audit: zero known vulnerabilities at audit time. The optional all-extras environment has one accepted no-fix NLTK advisory through LlamaIndex and is not the runtime profile.

These values may be quoted only with the workload qualifier. They are not production or business-task success rates.

## Agent Benchmark metric definitions

`forge eval-agent --split <dev|holdout>` runs the frozen case set in `evals/agent_cases.json` against both runtimes. The definitions below are frozen so that later reliability work cannot silently change what a number means. Full design and the M9/M10 boundary are in [`NEXT_PHASE_PLAN.md`](NEXT_PHASE_PLAN.md).

**Benchmark cases and scorer probes are reported separately and are never combined into one ratio.** Seven cases carry `grade: "fail"`; they exist only to prove the scorer detects real deviations (missing tool, forbidden tool, wrong order, wrong arguments, tools called on a no-tool case, over the call ceiling, and `exhausted` claimed as `succeeded`). They are excluded from every headline metric — a report must never read as "the agent passed 54 of 61" when seven of those are negative controls. Headline fields are `benchmark_cases`, `benchmark_pass`, `scorer_probes`, `scorer_probes_detected`, and `grading_agreement`. Probes exist only in the `dev` split; `grading_agreement` is therefore `null` on the holdout split rather than a vacuous `1.0`, because scorer validity is unmeasurable without probes.

| Metric | Definition | Decided by |
| --- | --- | --- |
| Task success | `status == succeeded` **and** every hard assertion in the case's `expected` block holds | Harness only; no model self-assessment |
| Tool set accuracy | Overlap between the called tool **set** and `expected.tools` (empty set required for `no_tool` cases) | Case contract |
| Tool sequence match | Called sequence equals `expected.tool_sequence`, including repeats and order | Case contract |
| Argument accuracy | Subset match on `expected.args_contains`, aligned **by call position** so a repeated tool is scored per invocation | Case contract |
| Forbidden tool violation | Any call to a name in `expected.forbidden_tools` | Case contract |
| Steps | `usage.steps`; a Plan-Execute run's planner decision is counted, so the planner cost is visible rather than hidden | Harness |
| Model decisions | Tool calls the model emitted, read from the trace; includes a final decision a budget gate then refuses | Trace |
| Logical tool calls | `usage.tool_calls`: calls **admitted** past the budget gates. This is the retry-independent count that `max_tool_calls` refers to | Harness |
| Tool attempts | Executions actually started | Harness (M10) |

Continuous metrics are reported for trend analysis. Only the hard assertions decide pass/fail, and `args_contains` is asserted as well as reported: a run with the right tools and wrong arguments must not be scored as a success.

**Step accounting across runtimes.** `RunBudget.max_steps` governs the executor loop, so a Plan-Execute run is granted one additional step for its planner and the step assertion is offset to match. Without this, `max_steps=1` would let ReAct execute one call while denying Plan-Execute any execution — the arms would not be comparable. The grant and the offset are explicit in `executor_step_budget` and `judge_case`. Every benchmark case therefore shows Plan-Execute spending exactly one more decision than ReAct.

**Two budgets, two names.** `executor_step_budget` is how many decisions the executor loop may spend; the `+1` planner grant lives there and is an *adapter contract* whose only purpose is to make the execution phases comparable. `total_run_steps` is the whole-run ceiling including planning, and is what an equal-resource comparison in M8-B must share across arms. Keeping the names apart prevents a fairness decision from being mistaken for plumbing, and prevents the adapter's grant from silently becoming the definition of equal resources.

**Report verdicts, and why `qualified` was split.** The report does not carry a single `qualified` flag, because one word was being asked to mean two different things. `measurement_valid` states that the experiment's data is structurally usable: at least one benchmark case graded, every category covered, an adequate holdout split in the manifest, and both arms satisfying their wiring contract. It says nothing about model quality, so a live run that fails many cases can still be a valid measurement. `keyless_gate_passed` is the keyless-only gate — every scorer probe detected and every benchmark case passed — and is `null` on a live profile, where the concept does not apply, rather than a misleading `false`. A live run reporting `measurement_valid: true, keyless_gate_passed: null` with a low pass count is a finding to investigate, not a broken gate.

**Dataset split and leakage control.** Cases carry `split: "dev" | "holdout"`. The holdout set is frozen and must not be used to tune prompts, planner instructions, or tool schemas; the manifest enforces a minimum holdout size. `make gate` runs the **dev** split only. The holdout set is run deliberately for live-model comparison, and per-case holdout results must not guide tuning — otherwise the split exists in name only. The keyless `dev` report is the checked-in release evidence; keyless holdout runs are exploratory and are not retained as evidence.

**What the keyless profile does and does not prove.** It validates the grading contract, the metric pipeline, negative probes, the wiring of **both** runtimes, and budget interaction. It does **not** compare ReAct against Plan-Execute: a scripted model replaces the decision under test, so both arms would differ only in budget and wiring. Strategy claims require live-model runs of both runtimes over the same cases, reported as two profiles — equal-resource and natural-cost — because a fixed identical budget charges Plan-Execute an extra planner call. Keyless token figures are synthetic: the report sets `token_source: "synthetic"` and `cost_metrics_valid: false`, and those figures appear only under `pipeline_diagnostics`, never as headline cost metrics. Local OMLX usage is never converted into a monetary figure.

## Trace step projection

`forge trace-steps <task-id>` projects a persisted, hash-verified trace into ordered step records. `forgeharness.observability.steps` is the project's **single** trace→step projection: the agent evaluator reads its step facts from the same code path, so a diagnostic view and a benchmark verdict cannot disagree about what a run did.

Identity is frozen ahead of retry support (M10):

| Field | Meaning |
| --- | --- |
| `logical_call_id` | One tool-call **decision** the model emitted; stable across retries |
| `attempt_id` | One actual execution attempt (`<call_id>#<n>`) |
| `step_id` | Display order only — never use it to join records |

Today one `logical_call_id` maps to exactly one `attempt_id`. M10 extends that to one-to-many; the field names do not change.

Two latency figures are reported and never conflated. `model_latency_ms_derived` is computed from the gap between a `model.request` event and its action, and is named "derived" because no model SDK reported it. `tool_latency_ms` is measured by the dispatcher around the tool call.

`budget_snapshot` records the remaining steps, tool calls, and tokens after each step, so an exhaustion is diagnosable directly rather than inferred — for example a planner consuming one step, then a third model decision being refused with zero steps left.

Two call sequences are exposed because they genuinely differ: `tool_sequence` is the **decision** order including any call a budget gate refused, and `executed_sequence` is what actually ran. An opened call with no completion projects as `incomplete` and is never silently upgraded to success; a call held for approval projects as `awaiting_approval` rather than as an unresolved gap. Projection is deterministic — the same events always produce byte-identical records — and a trace whose hash chain does not verify is refused outright.

**Profiles.** `keyless` is script-driven and gates the evaluator and both runtime wirings. `equal` and `natural` run a real model through the identical evaluator, projection and grading contract, and are the only profiles whose numbers may support a ReAct / Plan-Execute comparison. `equal` gives both arms one shared whole-run budget with **no** planner grant; `natural` gives both arms a generous budget so planner overhead appears in measured cost rather than in truncation. Each report records the budget every arm actually received under `budgets`, so the two profiles cannot be confused after the fact. Live profiles require repeated sampling (at least two); `judged_pass` requires every sample to pass, `pass_rate` retains partial credit, and numeric metrics are the per-sample median.

**Local live runs are resource-bounded.** A local model server reserves memory per request, so an unbounded per-request output ceiling lets a long run exhaust a 24 GB unified-memory host; `live_eval_config` therefore always sets `max_tokens` (`LIVE_MAX_OUTPUT_TOKENS`) and disables thinking. `--limit N` runs only the first N cases and marks the report `complete_split: false`, which forces `measurement_valid: false`: a truncated run is exploratory and must never be cited as a measurement.

**Live runs cover quality cases only.** Two kinds of case are excluded from a live profile because they test the harness rather than the model. Scorer probes are negative controls whose expectation the harness deliberately makes unsatisfiable, so grading them measures nothing and would report a meaningless detection rate. `budget_boundary` cases assert a specific `EXHAUSTED` outcome under a per-case budget override, while a live profile deliberately applies one shared profile budget so the arms stay resource-matched — under a live profile those expectations are unsatisfiable by construction and would add always-failing cases that dilute the comparison. Consequently `grading_agreement` is absent on a live report (the same "no vacuous 1.0" rule as a probe-free split), and category coverage is judged over the categories a live profile actually runs.

**Wiring checks must not encode quality.** The contract checks assert that the harness drove the runtime it claims to have driven, so the planner check counts planner **invocations** (`planner_requests`), not valid plans (`plan_events`). A live planner that answers with prose is a quality failure that the grading already records; reporting it as a broken contract would blame the harness for a model defect and hide the real signal. For the same reason a planner that raises still consumes its step: charging it only on success would make the arms' step accounting depend on model quality.

**Assertion fairness across runtimes.** A case's `max_steps` and `max_tool_calls` describe the **executor phase** — how many decisions it should take to *act*. Planning is a fixed structural cost of a strategy, not an acting choice, so the planner decision is offset in every profile. Without that offset a `max_steps=1` no-tool case is unsatisfiable for Plan-Execute regardless of its behaviour, which would score the strategy on having a planner rather than on acting well. Equal-resource pressure is expressed through the shared budget instead: a run truncated by that budget reports `EXHAUSTED` and fails its status assertion on its own merits.

## Bad Cases

A failing sample is captured automatically as a `BadCase` record under `reports/bad-cases/`, together with the raw hash-chained trace it points at. The record is bound to its origin (`revision`, `profile`, `arm`, `model`, `sample_id`) and to its evidence (`trace_path`, `trace_hash`), so a number can never float free of the run that produced it. `--only-cases <ids>` reproduces one failure cheaply instead of re-running a whole split.

**Artifact identity is `{profile}-{arm}-{case_id}-s{sample_id}`.** All four components are load-bearing: the same case can fail under `natural` and pass under `equal`, and repeated sampling can fail on one sample only. Without them an earlier capture would be silently overwritten, and evidence that overwrites itself is not evidence.

**Classification is structural, not per-case.** A failure is classified from evidence, in the order: a recoverable model-boundary violation the harness escalated (`protocol_violation` / `model_adapter`), any other boundary termination (`harness_protocol`), then a completed run that missed its contract (`model_quality` / `grading`). The provider's stable `error_code` drives the decision, never a human-readable message, so rewording a message cannot change a verdict. A `case_design_note` records a contributing weakness in the case itself, kept separate from the primary class so neither observation is lost.

**No-progress is a strict claim.** It is reported only when a tool is called with an identical name, identical canonicalised arguments, **and** an identical observation for every repeat. Repeating a call whose results differ may be legitimate progress, so identical arguments alone are never enough. This rule already prevented one wrong classification: `retr-find-then-read` looks like repeated searching but its queries differ (`retry policy` / `retry` / `max_retries`), so it is model quality, not a stall. `repeated_call_signature`, `repeat_count`, and `observation_hashes` are recorded so a later fix can be verified against numbers rather than by reading a trace by eye.

**Replay is not implemented.** `replay_mode: recorded_model_candidate` declares a candidate channel, and `replay_eligibility` states what a recorded replay could reproduce — neither asserts a capability. When it lands, a recorded replay will hold model choices constant to prove a *harness* fix, while a prompt or planner change will require a live re-evaluation; the two are never interchangeable.

## Final release gate

`forge review` rejects missing, undersized, revision-mismatched, or failed reports; unresolved blocker/high findings; a stale source snapshot; insufficient pure branch coverage; or an audit decision other than `passed`. A source snapshot cannot retroactively prove that an older real-model report executed that exact source. The current full-plan audit is held open until these provenance and scope gaps are resolved. See `docs/STATUS.md` and `docs/review/AUDIT.md`.

# Evaluation plan

## Current evidence

`evals/control_cases.json` is the immutable manifest for the current keyless suite. `forge eval-control` executes all cases and writes `reports/control-eval.json` with per-case outcomes, timing, usage, the manifest SHA-256, and the Git revision visible when the report is created.

The control suite currently covers normal tool completion, unknown-tool recovery, tool-budget exhaustion, model failure, approval suspension, trace tamper detection, and memory review gating. Fixture repair is also exercised as an integration test. No real-model or SWE-bench result is claimed yet.

## Questions

1. Does the Harness complete tasks reliably rather than merely produce plausible text?
2. Which context strategy improves success per token?
3. Does a reviewer sub-agent improve patch quality enough to justify its cost?
4. Do policy, checkpoint, and timeout controls behave correctly under failure?

## Datasets

- `control`: deterministic Harness-control cases for invalid actions, budgets, approval binding, resume, timeout, redaction, and child scope.
- `fixture-repair`: small local Git repositories with known defects and hidden acceptance tests.
- `swebench-sample`: a pinned, disclosed subset of SWE-bench suitable for the available compute budget.

Dataset manifests must include source, license, revision, selection procedure, and expected tests. Evaluation inputs are immutable after an experiment begins.

## Metrics

- resolved rate and pass@1;
- tests passed and regression count;
- patch validity and reviewer acceptance;
- model input/output tokens and estimated cost;
- wall-clock latency and tool execution time;
- steps, retries, timeouts, malformed actions, and denied tools;
- context utilization and retrieval recall on labeled fixture files.

## Required comparisons

- minimal linear context baseline versus budgeted context compiler;
- lexical retrieval versus lexical plus symbol retrieval;
- single agent versus reviewer sub-agent;
- at least two model configurations when credentials and budget permit.

## Reporting rules

- Separate keyless deterministic control results from real-model results.
- Report every attempted task, including infrastructure failures and refusals.
- Never compare costs without documenting pricing date and assumptions.
- Do not call a difference an improvement without reporting sample size and per-task outcomes.
- Store configuration and revision identifiers with every report.

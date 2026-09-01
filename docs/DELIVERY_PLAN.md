# Delivery plan

Status at the current audit candidate: M0–M6 are implemented with the limits in [`STATUS.md`](STATUS.md). M7 passed for revision `e3a8a38f5fdb4becdf7ff61fb349ce721be3e169`; findings and residual risks are recorded under [`docs/review`](review/AUDIT.md).

## Delivery strategy

Work proceeds in vertical, testable increments. Each milestone must leave the repository runnable and must not document planned behavior as implemented behavior.

## M0 — Contract and repository foundation

Deliverables:

- product, architecture, evaluation, review, and ADR documents;
- Python package, quality configuration, CI, and contribution commands;
- typed task/action/result contracts and deterministic test doubles.

Exit criteria:

- `make check` passes without a model key;
- the status documentation clearly separates implemented from planned behavior.

## M1 — Minimal Harness loop

Deliverables:

- ReAct loop with model/tool alternation;
- terminal states and step/tool/time budgets;
- validated tool registry and structured trace events;
- deterministic scripted-model tests for success, failure, exhaustion, malformed actions, and timeout.

Exit criteria:

- a keyless demo calls a safe tool and terminates with a reproducible trace;
- no loop decision relies on parsing free-form logs.

## M2 — Policy, approval, and durable state

Deliverables:

- normalized policy decisions: allow, deny, require approval;
- approval suspension/resume tied to exact normalized arguments;
- SQLite checkpoint snapshots;
- secret redaction and tamper-evident trace persistence.

Exit criteria:

- an unsafe write is suspended, cannot be approved for different arguments, and resumes exactly once;
- restart tests prove that completed side effects are not repeated.

## M3 — Context engineering

Deliverables:

- sectioned context compiler with explicit token budgets;
- repository map, lexical retrieval, Python symbol extraction, and provenance;
- history/observation compression;
- context manifest in each trace.

Exit criteria:

- overflow and relevance selection are deterministic in tests;
- errors and cited file locations survive compression.

## M4 — Coding-agent vertical slice

Deliverables:

- explorer, editor, tester, diff inspection, and completion evidence;
- scoped filesystem/search/process tools;
- user-selected Git workspace and fixed argv subprocess runner;
- keyless fixture repositories with known defects.

Exit criteria:

- a deterministic model fixes a fixture defect and emits a test-backed patch;
- unsafe path traversal and host writes are rejected.

## M5 — Skills, MCP, and sub-agents

Deliverables:

- versioned Skills loader with capability declarations;
- MCP client adapter using the native dispatcher/policy/trace path;
- parent/child budget accounting and reduced sub-agent tool scopes;
- single-agent versus reviewer-sub-agent experiment.

Exit criteria:

- MCP and native tools have equivalent validation and trace evidence;
- a child cannot exceed the parent's remaining budget or tool scope.

## M6 — Product surface and evaluation

Deliverables:

- CLI and FastAPI endpoints for local runs and trace verification;
- trace/context inspection UI remains deferred until core APIs stabilize;
- fixed evaluation dataset, baseline adapter, graders, and experiment reports;
- reproducible demo and resume-evidence document.

Exit criteria:

- reports disclose model, configuration, dataset version, failures, and cost assumptions;
- no resume metric is hard-coded or manually copied without provenance.

## M7 — Audit candidate and final review

Deliverables:

- frozen audit candidate;
- architecture, correctness, test, security, dependency, documentation, and claim reviews;
- defect fixes with regression tests;
- residual-risk and implemented/planned boundary report.

Exit criteria:

- all blocker/high findings are closed;
- `make review` passes from the reviewed revision;
- final claims link to reproducible report artifacts.

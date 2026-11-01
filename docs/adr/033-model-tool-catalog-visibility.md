# ADR 033: Per-call tool catalog visibility in the read-only kernel

Status: accepted, 2026-10-26. Scope: SDK 0.8.5 and the local strategy experiment.

## Context

The first 27B development pilot exposed registered material-reading tool schemas
in every model request. Fixed-flow synthesis and the three-role planner/worker/
reviewer accept final text only; they could nevertheless receive automatic tool
calls. Such actions were rejected and charged, but the experiment wiring gave
these phases an unusable action space. The interrupted pilot and all traces are
retained as instrumentation diagnostics; formal evaluation had not started.

## Decision

Add the boolean `RunKernel.call_model(include_tools=True)` keyword. False makes
this request's ModelRequest.tools empty before canonical preparation. The default
continues to expose the registry. Do not mutate the registry, adapter payload after
preparation, policy, permissions, validator, budget or settlement. Invalid flag
values fail before inference. Trace records the actual request, including omitted
schemas, and provider usage is charged by the existing shared invocation path.

The fixed final call and all three multi-role final-text calls pass False. Their
orchestrator still invokes the registered read tool deterministically, subject to
existing policy. ReAct keeps the default catalog and controls its own reads. These
are orchestration differences and are disclosed in the experiment protocol; the
comparison does not claim identical model-visible prompts or tool catalogs.

## Consequences and validation

The API is additive; state schemas and arbitrary-kernel read-only limits stay
unchanged. Catalog omission cannot grant tool authority. A model can still return
an invalid action, which retains the existing failure/accounting semantics.

Regressions exercise real adapter preparation under MockTransport, concurrent
hidden/default requests, trace integrity, exact usage, registry preservation,
non-boolean rejection before cost, denied writes and every benchmark final-only
phase. Candidate-r3 must pass all source and clean-copy engineering gates. Existing
R2 runs keep their bound wheel because framework version is part of run identity.

Repeat the complete development set and seal a new source-bound protocol before
270 formal samples. Corpus, external exact grading, budgets and predeclared
selection thresholds are unchanged. Mini-task results cannot establish repository
repair quality, human onboarding performance or native framework superiority.

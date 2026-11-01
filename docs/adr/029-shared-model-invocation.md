# ADR 029: one shared governed model invocation, split SDK modules

Date: 2026-10-20. Status: accepted for implementation. Implements the first half of
the F1c program defined by ADR 026; the tool/approval executor remains ReAct-only.

## Need

Two model-call implementations had drifted:

* `AgentRuntime._drive` traced `model.failed` with `error_code`,
  `received_tool_calls`, `recoverable` and the rejected request's `usage`, and
  charged one step plus the rejected tokens for a recoverable protocol violation
  (asserted by `test_replay.py`).
* `RunKernel.call_model` dropped all of it: a violation raised with only an
  exception type name and charged nothing, so a custom orchestrator that retried
  inside its budget understated real cost — the review's P0-4 finding.

Separately, `sdk/application.py` had grown to ~970 lines mixing four
responsibilities (contracts, kernel, bridges, Harness), which the review flagged
as P1-7.

## Decision

### Shared invocation core

`runtime/model_call.py` owns exactly one governed model call:

```python
async def invoke_model_once(*, model, request, budget, assembler, trace,
                            usage, phase) -> ModelCallOutcome
```

* Preparation, request budgeting, the canonical trace sequence
  (`context.overflow` / `model.preparation_failed` / `context.compacted` /
  `model.request` / `model.failed` / `model.action`) and **all token accounting**
  live here. Every payload carries `phase`.
* Accounting rules (single home, previously duplicated): success charges one step
  plus provider tokens; a recoverable protocol violation charges one step plus
  the rejected request's tokens; a non-recoverable failure or preparation failure
  charges nothing.
* `ModelCallOutcome` classifies the result (`SUCCESS`, `CONTEXT_OVERFLOW`,
  `PREPARATION_FAILED`, `MODEL_FAILED`, `PROTOCOL_VIOLATION`) and preserves the
  violation exception and post-call cumulative usage.
* **Loop policy stays with the caller**: violation feedback text, occurrence
  counting, limits, termination and checkpointing remain in `AgentRuntime`;
  `RunKernel` raises typed errors (`OrchestrationError` with the stable violation
  code) and lets the orchestrator decide whether to retry.

Both `AgentRuntime._drive` and `RunKernel.call_model` now consume this function.
`plan_execute.py` keeps its separate planner flow (`plan.created`/`plan.failed`)
and is explicitly out of scope.

### SDK module split

| Module | Contents |
|---|---|
| `sdk/spec.py` | `ScopeError`, `ApplicationComposition`, `ApplicationSpec` |
| `sdk/kernel.py` | kernel exceptions, `ModelBridge`/`ToolBridge`/`TraceBridge` (formerly `_Model`/`_Tool`/`_Trace`), `RunKernel`, `Orchestrator` |
| `sdk/harness.py` | `binding_digest`, `Harness` |
| `sdk/application.py` | pure re-export shim for the 14 existing import sites; removed at API v2 |

`forgeharness.sdk` imports from the canonical modules. Dependency direction:
`harness → kernel/spec`; `kernel → spec`; no cycles; `runtime` never imports
`sdk` (`model_call.py` lives in `runtime` and is consumed by both).

## Compatibility

* ReAct trace event names and asserted payload keys are unchanged; the unified
  events gain a `phase` key on `context.overflow`, `context.compacted`,
  `model.failed` and `model.action` (no test compared whole payloads; the
  golden suites `test_runtime`/`test_replay`/`test_request_preflight`/
  `test_plan_execute` pass unmodified).
* Kernel error messages: non-recoverable failures keep
  `model call failed: {type}`; violations now surface as
  `model protocol violation: {code}` and are charged.
* `ApplicationSpec` shape and construction are unchanged; the bridge renames are
  package-internal (no external references existed).

## Verification

* New `tests/unit/test_model_call.py`: success charging, recoverable violation
  charging with `model.failed` usage payload, non-recoverable and generic
  failures charge nothing, context overflow classification, `phase` tagging.
* New kernel test: a violating model fails the custom run with
  `model protocol violation: {code}`, `usage == (1 step, 80, 12)` — the same
  amounts `test_replay.py` asserts for ReAct — and the trace carries the rejected
  usage.
* Full suite, Ruff and mypy green; `sdk/application.py` is 30 lines, `loop.py`
  shed ~100.

## Consequences

* Cost accounting can no longer drift between orchestration styles; adding a
  third orchestrator inherits the same rules for free.
* F1c-2 (shared tool executor: approval admission, journal, retries, and custom
  run suspension/resume) is now the only remaining split governance path; ADR
  026's gate on write tools stays until it lands.

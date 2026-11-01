# ADR 026: custom orchestrator write path stays on ReAct for now

Date: 2026-10-18. Status: accepted; F1c implementation deferred.

2026-10-23 update: this boundary still applies to arbitrary one-shot `Orchestrator`
coroutines. The separate opt-in explicit R2 graph is implemented under [ADR 031](031-durable-orchestration-r2.md);
its qualification belongs to its own immutable candidate report.

## Need

`RunKernel` currently supports model calls and read-only tool calls. A custom
orchestrator that needs a write tool (for example a commerce ticket, a legal note, or a
file edit) must either use ReAct or be denied. The question is whether to add write
tools, approval suspension and invocation-journal recovery to the kernel now.

## Options considered

1. **Copy the approval/journal logic into `RunKernel`.**
   Rejected. It creates a second execution contract. The ReAct path's approval
   consumption, invocation journal, retry classification and crash windows are
   carefully ordered; a copy would drift and could silently weaken recovery.
2. **Call a nested ReAct run from `RunKernel.call_tool`.**
   Rejected for this API. It creates nested checkpoints, nested budgets and ambiguous
   parent/child evidence. The Harness already has a workflow abstraction for ordered
   child runs; a tool call is not a workflow.
3. **Extract a shared governed tool executor from `AgentRuntime`, then use it from both
   paths.**
   This is the viable F1c design. It requires moving approval admission, journal
   bookends, retry classification and checkpoint ordering behind a shared interface.
   It is a multi-day, high-risk refactor and needs its own ADR/tests.
4. **Keep the custom path read-only until (3) exists.**
   Chosen for API v1.

## Decision

For API v1, custom orchestrators are read-only and one-shot:

* `RunKernel.call_tool` rejects `RiskLevel.WRITE`/`PROCESS`/`NETWORK`.
* A policy decision of `REQUIRE_APPROVAL` is rejected with `KernelToolDenied`; it is
  not silently treated as ALLOW.
* `Harness.approve` and `Harness.resume` reject custom-orchestrator runs.
* A custom run is a one-shot; it has no approval delegation, journal recovery or
  resume claim. `RunResult.recovery_semantics_version` remains 0.
* Write/approval/journal/recovery remains on the ReAct path, which already owns those
  contracts.

This is an explicit capability boundary, not an accident. It is tested.

## Required work for F1c

Before write tools are allowed:

1. Extract approval admission and invocation-journal execution from `AgentRuntime`
   into a shared executor used by both ReAct and custom orchestrators.
2. Define custom-run suspension/resume semantics, including what the orchestrator
   replays after approval and how completed nodes are skipped.
3. Add fault-injection tests for the same windows ReAct covers: consume-before-
   checkpoint, started non-idempotent tool, changed policy, changed args, duplicate
   approval, and crash between tool completion and checkpoint.
4. Only then allow `RiskLevel.WRITE` and policy `REQUIRE_APPROVAL` in the kernel.

## Verification

`tests/unit/test_orchestration_kernel.py` covers:

* a write tool is rejected before execution;
* a policy-required approval is denied, not bypassed;
* custom-run `resume` and `approve` are rejected;
* read-only tool calls still work through policy/dispatcher.

## Consequences

* Legal read-only DAGs can proceed now (F2a/F2b).
* Commerce write/approval flows remain on ReAct until F1c.
* No second recovery contract is introduced; the cost is an explicit two-path
  limitation, documented rather than hidden.

# ADR 030: Shared tool invocation mechanics

Date: 2026-10-23. Status: implemented; qualification evidence is recorded separately.

## Decision

`runtime.tool_executor.ToolExecutor` owns policy-decision tracing, execution allowances,
dispatcher calls, invocation claims/attempts/settlement, retries, recovery decisions and
nested usage accounting. `AgentRuntime` delegates its existing invocation methods to
this object, preserving its checkpoint and approval-consumption ordering. The one-shot
`RunKernel` uses the same executor without an invocation journal and permits only
tools declaring both READ risk and READ_ONLY effect.

The caller persists admission before dispatch. The executor does not create a second
approval ledger, checkpoint store or orchestration state machine. ReAct still owns
pending approvals, exact approval consumption, authorized-call checkpointing and
continuation. Completed and indeterminate journal results retain their existing
recovery semantics, stable logical IDs and idempotency keys. Physical attempts retain
their own durable counts and backoff. Existing no-journal callers make no durable
recovery promise.

Policy evaluation and dispatch remain separate internal operations so ReAct can
persist an approval suspension or authorized admission before any side effect. Hosts
must use Harness/kernel APIs; the executor is not an unrestricted public SDK tool
entry point. Dispatcher argument validation and live contextual policy remain in
force. The caller owns `tool.started`/`tool.completed` and evidence; the executor owns
invocation/attempt events, so neither is emitted or billed twice.

Kernel model and tool admission/settlement share an in-process lock, preventing
concurrent calls from spending the same allowance or replacing each other's usage.
Nested tool steps/calls/tokens count identically to ReAct. This deliberately serializes
admission/execution; it is not a durable parallel-join protocol or a performance claim.
Tools must not recursively call the same kernel while holding its invocation slot.

## R2 boundary

Custom write/approval/resume remain rejected under ADR 026. R2 must explicitly
persist orchestrator version, stable node ID, input digest, completed evidence,
pending approval, cumulative budgets and unresolved effects, and define single-run
ownership and join semantics. It may compose this executor and existing journal,
but cannot reconstruct arbitrary Python coroutines from message checkpoints.

An unknown non-idempotent invocation is reconciled rather than automatically
replayed. A soft admission deadline does not mean an in-flight side effect rolled back.
Old state formats require explicit compatibility or rejection. Framework contract
substitutes do not qualify a host's business transactions or job/session projection.

## Verification

Preserve ReAct tool, approval, retry, journal, recovery, Coding and commerce tests.
Kernel regressions cover effect declarations, concurrent model/tool budgets and
nested usage. Full source/clean-copy, build/install, policy and dependency gates
must bind the final candidate before it is used as a qualified artifact. Existing
0.6.0 reports do not apply to this new source.

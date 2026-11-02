# 049 — A transient failure class whose recoverability follows the effect class

Date: 2026-11-02. Status: accepted.

## Context

The tool-failure taxonomy had four codes: `path_not_found`, `invalid_arguments`,
`timeout`, and the fallback `tool_execution_error`, which is non-recoverable because
the harness cannot promise that an uncharacterised failure is worth repeating.

In the DeepSeek run, `recover-error-retry` asks the model to "retry once if the
backend reports a transient error". The benchmark's own injection mode is documented
as transient, but it raised a bare `RuntimeError`. The harness therefore reported
`tool_execution_error` with `recoverable: false`, the model obeyed and did not retry,
and the case failed in all nine ReAct samples across the baseline, diagnosis and
improved runs. The same would happen to a
real tool whose backend drops a connection: the model would be told the failure is
final.

## Decision

- Add `ToolErrorCode.TRANSIENT` (`transient_error`) and `TransientToolError`, which a
  tool raises when its backend failed temporarily. `ConnectionError` is classified the
  same way.
- Recoverability is decided from the tool's declared effect class, not from the
  exception: read-only and idempotent tools report `recoverable: true` with a
  suggestion to retry once; a non-idempotent tool reports `recoverable: false`,
  because a write that failed mid-flight may already have taken effect.
- In the invocation journal, a transient failure of a non-idempotent tool settles as
  `indeterminate`, never as `failed`, so recovery cannot replay it.
- The runtime does not yet retry `transient_error` on its own. Doing so would change
  which attempts a journalled run makes, which is a new recovery-semantics generation;
  for now its retry decision is the same as for the unclassified error it replaces.
- The benchmark's `error` injection now raises `TransientToolError`.
  `BENCHMARK_VERSION` becomes 2 and `OBSERVATION_CONTRACT_VERSION` becomes 3.

## Consequences

A compliant model can recover from a flaky read without being told to ignore the
harness. Hosted tools that wrap HTTP clients must translate their client's transient
exceptions into `TransientToolError`; a raw client exception is still reported as an
unclassified, non-recoverable failure.

# ADR 035: Single-writer bounded model parallelism

Date: 2026-10-27. Status: accepted for implementation.

Follow-up: [ADR 036](036-scoped-model-invocation-identity.md) corrects invocation
scoping after PE-012. Final implementation uses SDK 0.9.1; the original 0.9.0
candidates remain historical and must not be treated as the corrected release.

## Context

R2 already owns durable DAG identity, budget reservations, owner fencing and an
outbox. Its runner awaits each node and contains shared mutable state. Running
`_execute()` concurrently would race those facts. A provider semaphore per run
would also fail to bound several runs using one local model.

## Decision

1. Keep a single owner and trace writer per run. Prepare requests synchronously
   through the common governed model-call core. Persist exact prepared inputs and
   a sending marker before spawning an immutable invocation ticket. Workers await
   only the provider and return an outcome plus a bounded in-memory event buffer.
   Only the owner commits state, trace, accounting and dependent admission.
2. Add an explicit `ParallelExecution` policy (one or two model requests, default
   one) and a host-owned `ModelDispatcher`. Parallel use requires the host to
   declare its model adapter safe for concurrent requests. Bind policy and
   dispatcher configuration to run identity. Tool execution remains exclusive
   against model requests in that run; final-only model permissions do not change.
3. A dispatcher owns one local provider capacity domain with a non-expiring POSIX
   file lock, bounded round-robin queues and a durable capacity ledger. All
   participating runs share it. Reserve a capacity ticket before root admission;
   mark that ticket sent before persisting the run's sending marker. Failure in
   this cross-database window can conservatively retain capacity, never admit an
   unrecorded provider call. There is no distributed or external-client guarantee.
4. Release unused capacity on proven preparation/admission failure. A returned
   canonical response confirms physical completion even if usage needs separate
   reconciliation. Transport failure, cancellation or owner death leaves sent
   capacity unresolved. Restart must not expire it: block new sending in that
   domain until authoritative reconciliation or a trusted host confirms the
   backend idle with evidence references. That administrative method is not a
   model tool. Unsent ledger tickets can be recovered without contacting a model.
5. Preserve component-wise `max(used, reservation)` for in-flight/unknown nodes.
   Known usage settles once, including invalid output and overrun. Revocation
   stops admission and disclosure, while an owner-fenced internal accounting path
   retains already incurred usage. Unknown calls never retry inference.
6. Failure stops new admission and drains active calls. Cancellation/deadline is
   polled while requests or capacity waits are pending; drain is bounded by policy.
   Unconfirmed calls become unknown. Worker cancellation is not proof of remote
   completion. A run with unresolved physical work cannot cross a tool barrier.
7. R2 state becomes schema 3 / recovery 5. Checkpoint envelope stays version 2;
   old R2 state is rejected, not inferred or migrated. Old wheels finish old runs.
   The SDK release is 0.9.0; defaults and the independent ReAct/RunKernel paths
   retain serial invocation behavior.

## Consequences and acceptance

Use the matrix in [the execution plan](../EXECUTION_PLAN_PARALLEL_EXECUTION.md).
Event barriers prove provider-entry overlap independently of timing. Process
faults must cover both sending ledgers and settlement/outbox boundaries. A new
candidate must pass source and clean-copy gates. Same-topology concurrency 1 vs 2
is a separate live-model experiment; backend batching, memory pressure and quality
can prevent a deployment recommendation even when the mechanism is correct.

The implementation remains Python 3.12/uv, framework independent and local POSIX
only. No new runtime dependency is required. asyncio cancellation behavior follows
the [Python 3.12 documentation](https://docs.python.org/3.12/library/asyncio-task.html#task-groups).

## Residual boundaries

Single dispatcher process per capacity domain; cooperating clients only. Domain
directory identity must be configured consistently by the host. Backend-idle
confirmation is a privileged host assertion, not automatic expiry. Unknown usage
still needs a receipt even after physical capacity is cleared. No concurrent tools,
autonomous subruns, dynamic graph, streaming or parallel workspace writes.

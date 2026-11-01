# ADR 041: Async hosted execution and complete PostgreSQL evidence

Date: 2026-10-29; updated 2026-11-01. Status: implemented; qualification in the batch report.

## Context

The qualified 0.11.0 PostgreSQL component stores governance facts and digests.
It does not store a resumable AgentRuntime transcript or dispatch providers.
Harness still owns local SQLite stores and an OS operation lock. Wrapping that
Harness in a PG lease would create two authorities and would not survive a
different worker directory. Replacing each synchronous CRUD store separately
would split approval consumption, reservation, checkpoint and evidence commits.

## Decision

Introduce a framework-independent `runtime.execution.AsyncExecutionBackend`.
The existing serial AgentRuntime retains the model loop, context assembly,
typed actions, policy, observations and verifier. Hosted checkpoints, atomic
approval admission, actual calls and recovery pass through awaitable operations.
The backend is mutually exclusive with the three local state stores. Local SDK
paths continue to use their existing SQLite interfaces. R2/parallel integration
is still a separate migration and cannot be inferred from this interface.

Prepare model inputs using the existing shared helper, then let the backend
persist a send permit before calling the original dispatch helper. Tool calls
continue through ToolExecutor/ToolDispatcher. Dispatch closures are single-use
and invalidated when the backend returns or suspends; a cached result cannot
retain a future send. Freeze mutable tool parameters before checkpoint awaits
and isolate the backend's copy from the dispatched copy. Unknown execution raises
`ExecutionSuspended`, rather than becoming a terminal zero-cost failure.

The backend owns `checkpoint.saved` and `approval.consumed` events within its
transactions. Core must not append an uncommitted tail after a terminal save.
This interface is trusted service code, not an API that untrusted clients or
models can implement or select by import path.

Add bounded `BufferedEvidence` using the existing TraceEvent/TraceEnvelope hash
algorithm and mandatory credential masking. Synchronous append only stages data;
checkpoint/send/result operations must await a database commit before confirming
the exact staged prefix. A complete snapshot can exceed the default 100-event
transport batch while remaining under 10,000 events/16MiB. Acknowledgement must
preserve events appended during the database await. Restore verifies the full
chain and a head supplied by trusted PG metadata; it never trusts a serialized
commit cursor. Reaching a limit refuses new work instead of silently dropping
model input or tool evidence.

Schema 2 adds complete initial input, original owner RunContext, immutable budget,
RunResult/CAS revision, full Usage projections, ordered evidence and physical-call
companions. New execution acceptance atomically writes its initial checkpoint and
scheduling outbox. It refuses to adopt an existing governance-only run. Current
hosted profile forbids physical workspace paths and requires one physical attempt;
logical workspace mapping and automatic retry need additional contracts.

Checkpoint updates lock run then checkpoint, recheck the lease after lock waits,
verify identity/revision and actual usage facts, and commit checkpoint, evidence
and outbox together. A trace reader locks checkpoint metadata for a consistent
history/head under READ COMMITTED. Unknown/reconciling state cannot be erased by
a stale in-memory RUNNING snapshot. Unresolved calls block suspension/termination.
Authoritative cancellation overrides a final/suspended result and clears obsolete
approval fields; success rejects scalar or full-Usage overspend and missing facts.
Legacy governance-only finish cannot bypass managed checkpoint/evidence commits.

Migration from schema 1 is explicit and transactional, with workers drained and
administrator grants republished. Runtime startup requires schema 2 and never
upgrades. Existing costs, unknown reservations, approvals and identities are kept;
new execution tables do not infer historical missing inputs. Candidate identity
must hash initial SQL plus ordered migration SQL, not initial SQL alone.

## Implemented and remaining boundary

The concrete PG backend and `HostedRuntime` now compose the serial runtime with
full-Usage admission/approval/checkpoint, immutable complete physical results and
receipt settlement, trusted deployment bindings, independent verifiers/resolvers,
and owner/reviewer/worker permissions. Context materials are scoped and compiled
on first use, then reused from the committed transcript after approval/restart.
Progress-based heartbeat expiry lets a paused worker lose its claim; actual late
facts remain appendable while control writes stay epoch-fenced.

Deterministic tool rejection has a separate immutable `execution_logical_usage`
fact. A fresh denial event and matching assistant/tool observation are bound to
the checkpoint transaction. Rejections consume tool-call budget but create no
physical invocation, receipt or unit charge. Admission and call caps count these
facts; checkpoint projections reconcile them with typed physical result/receipt
facts and integral units. Failed admission intents cannot later appear in a
terminal trace as committed admissions or consumed approvals.

An old-epoch admission still lacking a committed send permit is provably unsent:
under the current lease and run/call locks, retire it with an explicit fenced
nonexecution proof and zero actual usage. A sent or unknown call cannot use this
path. The controller stops that original run instead of fabricating a reusable
runtime output or silently retrying. Known costs survive cancellation/revocation.

Two-application, independent business-ledger and real loopback HTTP-adapter tests
exercise these paths, including missing usage, response loss, fresh-process
recovery and paused old-worker takeover. Candidate/clean-copy verification remains
required before qualification. This is still an internal trusted-host service,
not a public authentication implementation or a complete running SaaS product.

Do not use absence of provider usage, ToolOutput.ok=False, transport failure or
lease expiry as proof of nonexecution. Provider capacity, R2, nested calls,
automatic retries, sessions, identity/UI, billing and cloud operations remain
required parts of the master goal, with their own implementation and evidence.

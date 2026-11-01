# ADR 031: R2 durable, explicit orchestration

Date: 2026-10-23. Status: protocol frozen for implementation; qualification pending.

## Scope and compatibility

R2 is an additive SDK API v1 surface, `DurableOrchestrator`, with a finite,
versioned DAG of `DurableNode` definitions. A node is a tool call, a single model
request, or a deterministic join. Stable IDs include any host-expanded branch or
iteration identity. Unknown dependencies, cycles, duplicate IDs and arbitrary
coroutine stacks are rejected. Ready branches advance in definition order through
one governed invocation slot; this provides reconstructable fan-out/join semantics,
not simultaneous external execution or a throughput claim. A suspended or failed
branch does not erase independent branch results; joins require every dependency
to complete. Failure stops later admissions after the current scheduling sweep.

The distribution target is 0.8.3 (0.8.0 retained after six release fixture failures;
0.8.1 post-gate R2-005 found active input mutation; 0.8.2 post-gate R2-006 found a
zero-step node receiving one nested step; both original 92/92 reports are retained).
Every active admission/settlement now rechecks the complete initial identity, not
only the individual admitted call. No existing public
method is removed. `Harness.approve` adds an optional node selector. Existing
ReAct checkpoint/journal semantics and one-shot custom kernels stay unchanged.

| State | Inspect | Resume with R2 | Resume with old runtime |
| --- | --- | --- | --- |
| ReAct state v1 / recovery v1–v3 | existing scoped API | route to ReAct only; current v3 rules apply | existing contract |
| 0.7.1 one-shot state v1 / recovery v0 | existing scoped API | reject, no invented progress | reject |
| R2 envelope v2 / checkpoint v2 / recovery v4, DAG v1 | scoped API + R2 snapshot | exact definition/binding only | checkpoint v2 rejected by 0.7.1 parser |
| Future envelope, graph or recovery version | reject before serving output | reject | reject |
| LangGraph state | original runtime only | reject; no converter | original runtime only |

R2 checkpoints have a separate typed envelope in the same SQLite DB. A RunResult
with state_version=2 is a transactionally updated projection, not the source of R2
progress. SDK trace events remain v1; host event v2 is unaffected. Old code must
not be given an R2 state directory; its Literal[1] checkpoint parser also fails
closed. Existing ReAct/one-shot checkpoints stay v1. No implicit migration exists.

## Identity and authorization

The durable identity binds complete trusted RunContext, transformed task and
instructions, graph/model identity and version, tool specs, resource revision map,
application/policy version, framework distribution and budget. Same run with
changed identity is a conflict, including a changed budget or resource revision.
The host must supply a current `validate_resources` guard, called on history reads,
approvals, resume and every admission. Empty resource maps still require a guard.
This complements `ApplicationSpec.authorize_run` and live tool policy; none of
these callbacks may infer authority from model text. Host implementation changes
require application/graph/model identity version changes.

Tool call IDs derive from stable node IDs. Arguments are normalized with the
registered input model before binding. Model requests bind prepared input through
the shared model path and a stable node phase. Model outputs cannot dispatch
tools: a model node must return a FinalAction. Dependency outputs are untrusted
reference messages, ordered by declared dependency IDs. Completed node results
are reused only under the same complete identity and fresh authorization.

## State and transaction boundaries

Node states: `pending`, `awaiting_approval`, `admitted`, `unknown`, `completed`,
`failed`, `cancelled`. Run progress is running, awaiting approval, needs
reconciliation, or terminal. Unknown is deliberately not a successful or cancelled
effect. RunResult uses RUNNING + an explicit error for reconciliation; the R2
snapshot exposes the complete pending set and unresolved facts.

| Boundary | Atomic facts | Crash recovery |
| --- | --- | --- |
| Create | envelope, nodes, binding, start/deadline, first checkpoint, outbox event | existing request must resume; no second start |
| Suspend | exact normalized call and node approval state, checkpoint, event | pending collection rebuilt; no effect admitted |
| Admit | node input digest, full usage reservation, approval consumption (if needed), checkpoint, event | admitted with no journal may start without consuming again |
| Journal claim/start/attempt | existing ToolExecutor journal transactions | claimed may continue; started with missing outcome is unknown |
| Attempt settlement | physical attempt outcome and nested usage in existing journal | counts cannot reset at restart |
| Journal result | canonical tool output and invocation terminal state | rebuild missing node completion from this result |
| Node settlement | result, cumulative actual usage, released reservation, checkpoint, outbox | duplicate completion cannot charge twice |
| Finish | terminal result + checkpoint + outbox event | committed terminal state never regresses |
| Publish | append outbox event with stable run/revision event ID, then acknowledge | at-least-once; consumers deduplicate event ID |

Approval consumption reuses SQLiteApprovalLedger in the SAME transaction as R2
admission. There is no second approval grant table. Admission retains the consumed
grant, approver context, expiry and complete binding. If execution has not started,
resume rechecks the approver, expiry, policy and resources before dispatch, without
consuming twice. An expired or revoked admission cannot be revived with the same
grant; it requires a new run. Parallel approvals form a node-keyed collection;
issuing or consuming one cannot replace another. Cancellation invalidates pending
actions and unused grants by preventing all subsequent admissions.

## Budgets, unknowns and reconciliation

Each non-join node declares a maximum Usage reservation, including nested usage
and physical attempts. Model nodes require a step reservation. Tool nodes require
at least one tool-call reservation. Sum(used + outstanding reservations) must fit
the run budget BEFORE dispatch. Every physical tool attempt charges one tool call
plus its reported nested usage; each model request charges one step and reported
tokens. Attempts with unknown usage retain their entire reservation. Completed
results and repeated joins charge once. Actual use beyond the declared node or
run limit is retained, fails the run, and cannot be turned into success. Exactly
equal use is permitted. A zero token tool is permitted with zero token limits.

An admitted model node is marked unknown before invoking the shared model core.
If response persistence is lost, or a provider failure does not establish usage,
the reservation and unknown result remain. R2 never automatically resends that
request. Preparation/context rejection is known not to have sent and may release
its reservation. Protocol errors with reported usage retain that actual charge.

For tool recovery, existing completed journal results are reused. A claimed call
with no attempt may continue. A started attempt without a full outcome is UNKNOWN
even for idempotent/read tools, because unknown nested fees must not become zero.
R2 deliberately chooses reconciliation over automatic replay. A trusted host
reconciler must query the existing effect/receipt (or obtain explicit human
evidence) and supply the canonical output and total actual usage. It receives the
stable idempotency key; this is NOT permission to repeat a non-idempotent effect.
Journal resolution and node settlement share a transaction. No default resolver
fabricates success; without one the run stays blocked with its reservation.
Model unknowns require the equivalent authoritative response/usage reconciliation.

## Ownership, time, cancellation and lifecycle

Harness holds a nonblocking POSIX flock for the entire run/resume operation,
including awaits and retries. A new owner may acquire only after release or OS
process death; a hung living process is never taken over by timeout. Do not unlink
the lock file. Each operation rotates a persisted owner token; all state commits
check it and revision. Local filesystems with working flock/SQLite are the scope;
network filesystems, multiple hosts and distributed leases are unsupported.
Already issued external calls cannot be revoked by locking.

UTC start, last observed UTC high-water time and absolute deadline persist.
Approval waiting counts toward wall time. Backwards clock movement is rejected;
restart cannot renew the deadline. Deadline is an admission ceiling, not rollback
of an in-flight effect. Cancellation is a durable request that can be accepted
while the owner is active. Before every physical attempt and subsequent node the
owner reloads it. Accepted and terminated are distinct facts; in-flight work may
settle, and unknown effects retain their reservations and reconciliation state.
Terminal cancellation is committed only after unresolved effects have been handled.

Results, checkpoints, grants, journal and outbox are private local state. Trace
payloads pass the evidence transform and carry no credentials. History requires
fresh host/resource authorization. `Harness.delete` erases R2 state/outbox with
existing scoped evidence and retains the existing tombstone. Hosts own retention,
external receipts and domain transactions. E12–E16 remain separate host acceptance.

## Verification and estimate

P05 must map F01–F10 plus model-loss, usage, multi-approval and outbox windows to
deterministic subprocess barriers and persistent assertions. Existing ReAct,
Coding, commerce and one-shot tests remain required. Full source/clean/wheel gates
and an actual isolated law-office worker bind a unique immutable candidate.

Increment: additive public DAG/state contracts, a v2 checkpoint discriminator for
new R2 runs only, and attempt usage persistence. Existing state is not rewritten.
Estimate remains 8–14 person-days for the
whole batch until fault review supplies evidence. Implementer self-review is not
independent architecture or professional legal review. This ADR alone is not G3.

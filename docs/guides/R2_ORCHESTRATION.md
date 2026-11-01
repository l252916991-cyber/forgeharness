# R2 explicit orchestration

2026-10-27 update: SDK 0.9.1 adds [bounded model parallelism](PARALLEL_EXECUTION.md),
orchestration schema 3 / checkpoint 2 / recovery 5 and explicit provider capacity.
Current evidence is in `reports/parallel-execution-20261027/REPORT.md`. The 0.8.3
qualification and serial recovery-v4 statements below describe that historical
baseline; existing runs retain the wheel that created them. The graph and control
APIs remain available, with parallel configuration described in the new guide.

R2 is an opt-in SDK API v1 surface in the 0.8.3 candidate. Qualification belongs to
the unique candidate report, `reports/release-0.8.3-r1/verification/summary.json`;
this guide alone is not a passing G3 report. See [ADR 031](../adr/031-durable-orchestration-r2.md)
for transactions, migration rejection, budgets and failure windows.

`DurableGraph` declares stable tool, model and join nodes, their dependencies,
definition/model identities, resource revisions and a final node. Model nodes may
only produce a final action; model-authored tool calls do not execute. Expand any
bounded iteration into stable node instance IDs before creating the run. Invalid
or cyclic graphs are rejected. Existing arbitrary `Orchestrator.run` coroutines
remain read-only and one-shot; they gain no write or resume permission.

```python
from forgeharness.sdk import (
    DurableGraph,
    DurableNode,
    DurableOrchestrator,
    Harness,
    ToolCall,
    Usage,
)

graph = DurableGraph(
    version="review-v1",
    model_identity="registered-provider-and-config-v1",
    resource_versions={"case:synthetic": "revision-1"},
    nodes=(
        DurableNode(
            id="draft",
            kind="tool",
            max_usage=Usage(tool_calls=1),
            call=ToolCall(id="draft", name="registered_draft_tool", arguments={}),
        ),
    ),
    final_node="draft",
)
orchestrator = DurableOrchestrator(
    graph=graph,
    validate_resources=host_validate_current_authorization_and_revisions,
    reconcile_node=host_query_existing_receipt,  # optional; never blind replay
)
harness = Harness(registered_application, state_dir=private_state_dir, orchestrator=orchestrator)
```

The host guard must check every affected source revision, deletion, expiry and
permission, including compiled context materials, on each invocation. It also
protects history reads. The complete RunContext, task, graph, model identity,
tools, resources, budget and framework version bind the run. Change them by
creating a new run, never by silently reinterpreting a checkpoint.

Each non-join node reserves its declared maximum Usage before execution. Include
all physical attempts and nested usage in that allowance. Known actual usage
settles once; exact limits are allowed; an overrun fails while retaining actual
consumption. Unknown response/effect/usage retains its reservation. Model transport
failures never become a free restart. Tool retries use the shared ToolExecutor and
existing invocation journal; a stopped attempt cannot reset its physical count.

Use `inspect_orchestration(context)` for the full node/pending/unknown collection,
reservations, deadline and cancellation request. RunResult is a compatibility
projection; its `pending_approval` shows the first item only. Select an exact
`node_id` in `approve` when multiple nodes wait. Resume with that grant and a
freshly authenticated approver context. Consumption and admission share a SQLite
transaction. An admitted action that never started rechecks expiry/authorization
on restart without consuming the same grant again.

`reconcile(context=..., node_id=...)` invokes only the registered trusted resolver.
It must query an existing receipt or provider response using the stable key and
return `ReconciledNode` with canonical output, TOTAL actual Usage and evidence
references. Without authoritative evidence return None. It must never repeat an
uncertain non-idempotent action. Known results and known consumption cannot be
replaced or reduced. Resolved output and journal/node settlement commit together.

`cancel(context)` reports acceptance of a durable request, not proof that an
external action stopped. The owner blocks later admissions, retains submitted
receipts, and keeps unknown effects in `needs_reconciliation` until resolved.
Deadline is a persisted UTC admission ceiling; approval waiting counts and clock
reversal fails closed. A restart does not renew the deadline.

Ready branches advance deterministically through one invocation slot. Independent
results survive another branch's suspension, and joins require all dependencies.
There is no parallel throughput or distributed scheduling claim. A POSIX flock
remains held across await/retry; takeover requires its release or process death.
State writes also fence the owner token and revision. A live hung owner cannot be
stolen. Keep the lock file and use a local filesystem with working flock/SQLite.

Checkpoint v2 makes 0.7.1 reject new R2 projections. New state is isolated on
rollback; ReAct and one-shot v1 state is not rewritten or converted. SDK outbox
events have stable run/revision IDs and at-least-once delivery; deduplicate by
event ID. They remain SDK event v1 and do not change any host event-v2 protocol.
Deletion clears local R2 graph/outbox/journal/grants under the existing tombstone.
The host owns external receipts, domain transactions, retention and backups.

Run `uv run --no-sync python examples/durable_demo.py` for a synthetic write,
approval and fixed test command across three separate processes. It works with
the minimal installed wheel, needs no model or network, and writes only temporary
files. Actual host job/session mapping, business transactions and production
HTTP/UI integration require their own acceptance.

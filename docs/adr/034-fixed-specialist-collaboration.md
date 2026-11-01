# ADR 034: Fixed, read-only specialist collaboration on R2

Date: 2026-10-26. Status: accepted. Engineering qualification is recorded only in
`reports/multi-agent-20261026/REPORT.md` and its immutable candidate summary.

## Decision

Compile trusted, finite collaboration definitions into existing R2 model nodes:
one node per independently assigned specialist task and one dependent coordinator.
Each node makes one final-only model call with an empty tool catalog, through the
shared governed invocation path. All nodes use the application's registered model;
the host binds its provider/config identity in the definition. No new scheduler,
message bus, role-derived authorization or autonomous child execution is introduced.

Experts have stable registered identities and versions, instructions and declared
maximum usage. Delegations have independent stable task IDs even when reusing an
expert. Evidence snapshots carry resource IDs, revisions and text digests. Only
assigned evidence enters a specialist's messages; the root task is deliberately
public. Coordinator dependencies contain untrusted validated specialist outputs.
The host must authorize the execution binding and revalidate every resource.

Add an optional JSON output schema to R2 model nodes. Validate schemas before run
creation, reject external schema references, and validate final-action JSON before
marking a node complete. Validation also runs during reconciliation. Invalid
output is a known failed response with known consumption, not an unknown/free
retry. Existing unschematized nodes retain their behavior. Graph/framework identity
binds the new field; this release never resumes an old wheel's active R2 run.

Output contracts bind each specialist's task ID and allowed evidence references.
The final verifier validates report structure and source identity; semantic support
remains a separate host/domain assessment. Unknown response/usage retains the
existing reservation and trusted-reconciliation requirement.

## Inspect and persistence

There is one R2 root run with durable delegation nodes, not a collection of
independently resumable child runs. A task-tree projection combines the root run ID
and stable node ID into a unique delegation ID; it exposes status, actual usage,
reservation and parsed reports after the same authenticated inspect checks.
R2 checkpoints/outbox and model request/action trace remain the source of truth.

## Alternatives and consequences

Reusing SubAgentTool alone leaves its in-memory trace and repeated role-based child
ID unchanged and does not establish durable subrun recovery. A separate multi-agent
runtime would duplicate governance. Arbitrary dynamic fan-out and simultaneous
execution expand crash/permission/budget windows and require later ADRs.

The initial mode is serial, fixed, read-only and opt-in. It has no concurrent speed,
autonomous tool-loop, isolated-write, distributed scheduling or general quality
claim. No new runtime dependency is needed. Engineering verification uses scripted
responses; broad real-task evaluation remains a separately sealed experiment.

## Attribution

The orchestrator/worker pattern was discussed with reference to Anthropic's
[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents).
Implementation reuses ForgeHarness's own R2 and shared model invocation contracts;
no external implementation code is copied. The project's measured A/B/C results
motivate retaining explicit opt-in selection rather than a universal default.

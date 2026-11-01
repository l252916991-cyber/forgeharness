# ADR 013: shared business modules and durable workflows

Date: 2026-10-12. Status: accepted for implementation.

## Need

LexVault needs ordered retrieval/analysis/review, source locations, and approvals
that cease to authorize edited evidence. Commerce needs the same primitives for
knowledge, content drafts and training material, alongside current product APIs
and idempotent handoff. Neither domain belongs in the runtime.

## Decision

Add opt-in, dependency-light public modules under `forgeharness.sdk`:

* A local durable DAG coordinator composed from existing `ApplicationSpec` and
  `Harness` instances. It executes ready nodes sequentially in stable order;
  graph validation, bounded inputs, static whole-workflow budget reservations,
  parent binding, persisted definition digest, and stopped-worker recovery are
  explicit. Each node retains ordinary tool policy, approval, journal and verifier.
  No second tool dispatcher, model-driven scheduler, or LangGraph dependency.
* A scoped revision store with immutable content versions, optimistic updates,
  human review, explicit publication/withdrawal, freshness and an atomic audit
  history. Revision approval is separate from permission to execute a tool.
  Editing withdraws the previous published version immediately; rollback is a new
  draft requiring review. Hosts supply live authorization on every operation.
* Exact source anchors, published-record context providers, citation validation,
  and verifier composition. Citation validation proves a current scoped source
  and exact quoted span, not semantic entailment or legal applicability.

The reference legal and commerce modules use only public SDK contracts and
synthetic data. LexVault itself and the external commerce workspace are not
modified. The modules are preparatory building blocks, not a complete migration
or a finished commerce product.

## Reliability and boundaries

Workflow parent state and lifecycle events share a SQLite transaction. Stable
child IDs reconcile a crash between child completion and parent advancement.
Completed children are never rerun; stopped RUNNING children use Harness recovery;
terminal failures are not silently retried as a new run. Approval is delegated to
the current child with freshly authenticated identity and current policy checks.
Definition changes reject resume even if a caller forgot to bump the version.
Python implementation changes still require host-owned application version bumps.

Reservations sum each node's declared maxima, including nodes not executed after
failure. They prevent granting fresh independent budgets beyond the workflow cap;
unused reservations are not redistributed. Model usage semantics are inherited
from Harness. Sequential admission avoids adding parallel budget/side-effect races
before there is evaluation evidence for parallel scheduling.

All identity comes from authenticated host contexts. Stores and registered
adapters remain trusted code on one POSIX machine and local SQLite. Core credential
masking is applied before persistence; hosts add domain PII handling. Internal audit
chains do not provide external tamper attestation. Business stores have separate
retention/deletion from run evidence. No remote publication, payment, order mutation,
OS sandbox, distributed lease, general exactly-once, or legal correctness claim.

## Verification

Regression cases cover cycles/unknown dependencies, whole-flow budget rejection,
approval/restart, child/parent crash windows, active-worker conflicts, definition
changes, cross-scope access, stale revisions, revoked reviewer permission,
publication prerequisites, expiry, idempotent reads and citation tampering.
Run the existing Coding/commerce suites, new public-module integration examples,
full quality/evaluation/dependency gates and a clean-source equivalent verification.

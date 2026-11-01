# ADR 015: versioned material import, authorized retrieval and durable local tasks

Date: 2026-10-14. Status: accepted for implementation.

## Need

Phase S2 of `reports/MODULAR_NEXT_PHASE_PLAN.md` requires a public chain
"staging → validation → review → publication → searchable" over versioned material,
plus durable task state with progress, restart recovery and cancel requests. Today
`RevisionStore` (ADR 013) already owns draft/review/publish/withdraw with optimistic
concurrency and source dependencies, but there is no staging area, no parse step, no
index that is bound to a revision version, and no durable task record. The only
import/index implementations are the optional `knowledge` application, whose index has
no version, tenant or approval column, and whose `IngestJob` has no progress, no
cancel and no restart scan. Core code may not import it, so it cannot be the public
contract.

## Decision

1. Three new base-SDK modules, importing only the standard library and existing core
   modules: `sdk/tasks.py` (durable task records and runner), `sdk/imports.py`
   (staging and the import orchestration) and `sdk/retrieval.py` (an index bound to
   published revision versions). Parsers, embedders and business validators are host
   supplied through protocols, so the base SDK gains no media or network dependency;
   optional adapters may wrap pypdf, Pillow or a vector service behind the same
   protocol.
2. **Staging.** `ImportService.stage()` writes bounded bytes to
   `<state_dir>/staging/<scoped-id>` with an application/tenant/actor/content-bound id, a declared content type
   and size bound, and a retention deadline. A staged artifact is not a revision and
   confers no trust: it cannot be read as material, and publication happens only
   through `RevisionStore`.
3. **Chain.** stage → parse (host parser) → validate the exact parsed document (host validator) → `RevisionStore.save(..., request_id=<stable business key>)` →
   human review → publish → index refresh. Every step is idempotent on the business
   key: importing the same staged content twice returns the existing revision instead
   of creating a second one, and a rejected validation leaves a record, not a draft.
4. **Index.** An index entry carries `(application_id, tenant_id, resource_id,
   version, locator, span, digest)` and is built only from published revisions. A
   search takes the caller's current `RunContext`, restricts candidates to the
   authorized resources *before* scoring, then re-reads each hit's current published
   revision (version + digest) and re-checks host permission before the hit is
   returned. A stale, withdrawn or expired entry is refused, never served, and a hit
   is reported as a `SourceRef` so ADR 016's revalidation applies to it as well.
5. **Tasks.** `TaskStore` persists one record per `(application, tenant, request_id)`
   with a stable business key, kind, effect class, progress, attempts and state;
   `queued → running → {succeeded, failed, cancelled, needs_reconciliation}`. The
   runner claims exclusively, records progress, and settles through
   `runtime/retry.decide_retry` so retry policy is not re-implemented. A retry reuses
   the same task identity and business key, so a retry cannot create a second
   revision, index row or ticket.
6. **Cancel.** Cancelling a queued task settles it as cancelled with no handler
   execution. Cancelling a running task only requests cancellation: the runner stops
   before the next unit of work. Work whose outcome is unknown (a non-idempotent step
   that may already have been written) becomes `needs_reconciliation`, never
   "cancelled and done". Cancellation is not a rollback and is never described as one.
7. **Restart.** Task records are the authority. On startup the host enumerates
   non-terminal tasks and resolves each one against its durable artifacts — staged
   file, revision, index rows — then calls `reconcile()`; the framework never guesses
   an outcome and never re-executes a settled task.
8. **Retention.** Staging carries a deadline and a bounded sweep; parse output is
   either transient or promoted into a revision; index rows are pruned when their
   revision is withdrawn, superseded or deleted; run evidence keeps its own lifecycle.
   Deleting a revision withdraws its publication and drops its index rows. A sweep
   never removes evidence of a task that is not terminal.

## Consequences and alternatives

* Local, single-writer, POSIX/SQLite only. No distributed scheduler, outbox or
  cross-database transaction — the phase plan explicitly excludes them, and a real
  external write will need its own reconciliation design when it arrives.
* The staging area is owner-private and content-addressed, so a crash between write
  and rename leaves an orphan requiring host maintenance; a staged file never becomes
  visible material by existence alone.
* Rejected: extending the legacy `knowledge` tables in place. They carry no identity
  or approval columns and importing them into core would invert the dependency
  direction the architecture test enforces.
* Rejected: cancelling by killing the worker. That would make "executed but
  unknown" indistinguishable from "never executed", which is exactly the state the
  invocation journal exists to prevent.
* Residual: the index is a local index. Hosts that need hybrid vector retrieval supply
  an embedder and a vector store behind the same protocol; the framework does not
  claim semantic retrieval quality.

## Verification

`tests/unit/test_tasks.py` (task states, cancel semantics, retry matrix, restart
reconciliation, binding and single writer), `tests/unit/test_imports.py` (chain,
staging retention, idempotent re-import, rejected validation), `tests/unit/test_retrieval.py`
(version-bound candidates, withdrawal after indexing, scope restriction before
scoring, stale-entry refusal), plus the S2 acceptance script in
`docs/BUSINESS_PHASE_SCOPE.md`.

Tenant sweeps never perform global orphan deletion. Active-task retention is conservative
when task evidence is unavailable. Task results store references, not material bodies.
Deleted task identities and business keys remain tombstoned; they cannot be reused.

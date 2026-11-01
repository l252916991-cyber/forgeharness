# ADR 040: Optional PostgreSQL execution-state transactions

Date: 2026-10-29. Status: implemented first slice; qualification is candidate-specific.

## Context

Cloud must support several trusted workers without using a local process lock as
the source of ownership. Copying the SDK SQLite controllers behind async methods
would preserve machine-path identity and split approval, budget and run commits.
The existing knowledge application's PostgreSQL sessions are a different store.

## Decision

Add a separate, optional `forgeharness.cloud` package. Each operation owns one
database transaction: run acceptance plus durable event, lease claim plus epoch,
exact approval consumption plus reservation plus invocation, or receipt-based
settlement plus event. No external request executes inside a transaction.

Use composite tenant keys and FORCE RLS on all tenant tables. Bind transaction
local tenant settings after authentication by the host; recheck current grants
under shared row locks. These settings are defense against trusted-service query
mistakes, not authentication against a service with arbitrary SQL and credentials.
Only trusted service identities may execute or append independently verified
provider receipts. Do not expose receipt ingestion to tenant clients.

Provision schema and runtime grants explicitly through a migration identity.
Runtime startup never migrates. Reject roles able to become superuser, BYPASSRLS,
CREATEROLE or schema/table owner, including NOINHERIT memberships that permit
SET ROLE. Reject `current_user != session_user`, including privileged connections
disguised by a startup role. Row-lock privileges use an inert column; runtime
cannot change member permissions, modify receipts, or own the schema. This does not make a compromised
runtime role harmless: it still has trusted-service DML rights.

Run locks serialize reservations and epoch changes. Check database time after
acquiring a lock. Expired ownership never clears unresolved reservations.
After any later approval/invocation lock wait, recheck lease/deadline before new
admission or sending, and recheck lease before settlement/unknown projection.
An invocation with an immutable receipt cannot subsequently be sent; receipt
ingestion and sending lock the same invocation row to serialize this boundary.
Both acquire the run before the invocation; late receipt ingestion uses a key-share
run lock without requiring a current lease, avoiding outbox-FK lock inversion.
Commit send intent before calling a provider; an uncertain result must be reconciled
using its original identity, never resent merely because the lease expired.
An admitted but unsent old-epoch invocation also requires verified nonexecution
before releasing its reservation; automatic retry/resume is outside this slice.

Receipt facts are immutable and deduplicated. They can arrive after the original
worker loses its lease, but only the current owner advances projections or
settles. Record actual cost even if it exceeds the reservation or run limit.
Cancellation, tenant suspension and owner revocation do not erase spent cost.
Before sending a previously admitted call, recheck actual usage plus all remaining
reservations against the run limit. Admission does not promise a future send if
an earlier provider request later exceeds its reserved estimate.
Integral units are dimensioned by the immutable deployment binding; they are not
currency, subscription quota or customer invoices.

## Verification and dependencies

`cloud` installs asyncpg. Base SDK imports do not require it. `postgres-test`
locks embedded-postgres 18.6.3, whose real PostgreSQL 18.6 server is used solely
on a private Unix socket with disposable synthetic databases and separate runtime
roles. The runner must fail, not skip, if that server is unavailable. The binary
is not the production deployment or an assertion of production TLS support.

Upstream attribution: [embedded-postgres](https://github.com/kainpl/embedded-postgres)
provides the test-server lifecycle (Apache-2.0 wrapper; PostgreSQL binary/license).
Database behavior is exercised through asyncpg, not copied SQL emulation.

Source and clean-copy release gates append this real-PG coverage to core-test
coverage before enforcing both combined and pure branch thresholds of 85%.
An independent wheel consumer must load packaged SQL and finish a synthetic
approval/reservation/receipt/settlement flow. Historical source freezes remain
immutable; this is a new 0.11.0 engineering candidate.

## Consequences and unimplemented boundary

This supports only a foundational persistence contract. It is not connected to
Harness/SessionService, real providers, cloud identity, tenant API/UI, provider
capacity scheduling, checkpoints, logical-node attempts, full trace storage,
deletion tombstones, outbox delivery, billing or production failover. Those must
use the same operation-level invariants and receive independent fault gates.
Unknown calls may need operator reconciliation. M1 and GA remain incomplete.

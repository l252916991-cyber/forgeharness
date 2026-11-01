# 046 — Invitation control plane and current dispatch authorization

Date: 2026-11-01. Status: accepted for implementation; qualification is separate.

## Decision

The internal Alpha uses the execution PostgreSQL authority for platform identity,
organization/workspace membership, immutable deployment bindings, invitations,
sessions, authorization grants and audit. Authentication is a replaceable local
invitation adapter; it is not a production IdP. Global account/session lookup and
tenant business access have separate contracts and SQL privileges. No Cloud code
depends on the commerce application's SQLite identity authority.

Accounts have random stable identifiers. Existing actor strings and all schema-2
execution records retain their meanings. An upgrade never merges an old actor or
an email into an account, adopts an old run, or rewrites its owner. External
identity will bind by issuer and subject in a later independently qualified phase.
An existing account accepts another invitation using authenticated credentials;
acceptance cannot replace that account's password or other memberships.

Organizations contain workspaces. A deployment belongs to exactly one workspace
within an organization and binds one reviewed implementation digest. Membership
and deployment permissions are distinct. A new run records the workspace,
deployment, implementation digest and fixed resource scope in the same transaction
as its initial checkpoint and scheduling event. Broadening current permission
does not change that binding. Reusing an idempotency key with a different task,
workspace, deployment or binding conflicts.

## Permission matrix

| Operation | Subject and current checks | Scope and durable evidence |
| --- | --- | --- |
| Bootstrap | Offline migration/identity administrator | Explicit organization and owner; identity audit |
| Login/invitation/session | Active account; one-use invitation or password; durable failure throttle | Global identity lookup; token digests only; session absolute/idle expiry |
| Select organization | Active account and selected organization membership | Session bound to selected organization; identity audit |
| Member/workspace/grant management | Current owner/admin, rechecked under locks; last owner protected | Selected organization; audit of object identifiers and decision |
| Submit | Active account, organization, membership, workspace, workspace membership, deployment and use grant | Server-resolved scope/budget; run, checkpoint, binding and outbox atomically |
| Read/cancel/list run | Current scoped membership and grant; exact original owner | Requested organization/workspace must match durable binding; minimal DTO |
| Review/approve/reject | Current approval grant in the bound workspace; independent account; application policy | Exact pending action fingerprint; one durable decision per action; audit/outbox |
| Dispatch | Worker execute permission plus current original owner authorization and exact accepted binding | Admission and mark-sent transactions; committed trace and physical facts |
| Settle/reconcile | Trusted worker and immutable physical receipt | Existing sent/unknown facts remain settleable after owner/deployment revocation |
| Migrate | Separate offline owner, stopped writers, explicit inventory | Transactional 2→3; original migrations unchanged; no history adoption |

Organization ownership does not grant customer-content read, business approval or
worker execution. A human account is never assigned can_execute by the control
plane. Approval permission is projected into legacy members in the same identity
transaction as its authoritative grant. Workers cannot administer membership or
perform DDL. Web credentials cannot execute, settle or alter execution leases.
The shared SQL service is trusted code, not a hostile arbitrary-SQL sandbox.

## Transactions, rejection and revocation

Identity mutations re-read the caller, account and current role in their own
transaction; a Principal is an identifier, never a role snapshot. Authorization
locks serialize with revocation. New-sensitive-operation failure is closed when
PostgreSQL or authorization is unavailable. Dispatch enters the in-flight window
at the committed mark-sent transition; a revocation ordered after that transition
cannot retract an already entered network call. Logout only revokes the session;
membership/grant revocation also prevents background sends.

Approval and rejection compete on the same run lock and exact action fingerprint.
Only one immutable decision can win. Same-decision retries are idempotent; an
opposite decision conflicts. Rejection persists its own decision and requests
execution convergence, prevents new sends and grant consumption, and ultimately
produces a cancelled run with approval_rejected in its public error. It cannot
erase a receipt, reservation or unknown outcome. A worker reconciles previously
sent facts before reaching a terminal checkpoint.

Deployment deactivation prevents admission and sends. Authorized history reads
use persisted bindings and checkpoints without importing currently registered
code. Revoking a user's current workspace/grant still removes historical access.

## HTTP, worker and capacity contract

Public writes take only task, object selections and an idempotency key. Actor,
roles, scopes, budget, provider identity, imports and trusted grants are never
request fields. Organization/workspace/run path combinations are checked together.
Private responses use no-store; unsafe requests require an allowed Origin and
session CSRF token; hosts and CORS are explicitly configured. Cookies are HttpOnly,
Secure by default and SameSite strict. Errors are normalized and contain no SQL,
DSN, credential, checkpoint or arbitrary exception. Lists have bounded pagination.

API acceptance returns 202 after the atomic durable transaction. A separate
serial worker scans PostgreSQL, takes epoch-fenced leases and continues approved
runs; notifications are optional hints. The loop has bounded polling/backoff,
stops taking work on shutdown and exposes counters. Per-user/organization rate,
pending-count and retained-run ceilings bound this internal adapter. No unmetered
real-provider entry is enabled by the synthetic qualification. Integer execution
units do not implement a CNY monthly budget.

## Qualification and remaining boundary

New behavior requires unit, real-PG transaction, real HTTP/process and browser
evidence appropriate to the boundary, then a separately frozen source and clean
copy through all existing and new gates and a consumer of the same built wheel.
The historical 0.12.0 candidate and evidence remain unchanged.

Production IdP/MFA/recovery, machine credentials, monthly currency accounting,
multi-worker fairness/capacity, arbitrary code, public signup, real cloud
deployment/backup/SLO and real customer business are not delivered by this ADR.

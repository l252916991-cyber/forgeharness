# Invitation-only internal Alpha control plane

This is an application-neutral adapter over the PostgreSQL serial hosted runtime.
Qualification belongs to the frozen candidate report, not this document. It does
not enable public signup or establish production SaaS, production identity,
currency billing, cloud availability or real customer business qualification.

## Installation and composition

Use Python 3.12 and install `forgeharness[cloud,web]`. The hosted registry remains
trusted deployed Python: construct immutable `HostedApplication` registrations
with independent result verifiers, fixed scopes, policy and bounded budgets.
Request JSON cannot select Python imports, model accounts, roles or budget limits.

Provision three distinct non-owner, non-superuser, non-bypass-RLS PostgreSQL login
roles with no membership in a migration owner. Use the offline migration
connection with `cloud.provision.migrate`, then `grant_identity`, `grant_web` and
`grant_runtime` for the identity service, Web control service and worker roles
respectively. `migrate` creates schema 3; it is never called by service startup.
Credentials/DSNs come from local protected configuration, never task input or
trace. Do not log connection strings or raw tokens.

Create `PostgresIdentityStore.connect(identity_dsn)` and, offline, call
`identity.bootstrap(org_id, email, password)` once for a new organization/account.
There is no HTTP bootstrap endpoint. Store the returned session securely or log
in normally; its token is not a password-reset mechanism. Existing accounts join
another organization through an invitation while authenticated, with no password
field, and keep their original password and memberships.

Create `PostgresRunStore.connect(web_dsn)` and
`PlatformRuntime(web_store, registry, limits=AdmissionLimits(...))`. Mount
`create_control_app(identity, platform, allowed_origins=("https://host",),
allowed_hosts=("host",))` in an ASGI server. Exact origins/hosts are required.
Secure, HttpOnly, SameSite-strict cookies are the default; TLS is supplied by the
host's reverse proxy. `secure_cookies=False` is solely the explicit local HTTP
test adapter. The Web SQL role cannot update execution checkpoints, leases,
invocation states or receipts. Never substitute the worker's DSN into the Web pool.

Provision an organization-scoped service actor in legacy `members` with
`can_execute=true, can_approve=false` through the offline administrator. It is not
a human login or an API-supplied identity. In a separate process connect the
runtime role and construct `HostedRuntime(worker_store, registry)` or the same
`PlatformRuntime` registry. Run:

```python
stop = asyncio.Event()
worker = HostedWorker(
    runtime, TenantContext(org_id=org, actor_id=service_actor), worker_id=instance_id
)
await worker.run(stop)
```

Handle process termination by setting `stop` and waiting for current work to
finish. Hard process loss is recovered through expiring leases and epoch fencing.
The worker scans only platform-bound runs in its explicitly selected organization.
There is one execution at a time per loop. `tick()` handles at most one eligible
run and is useful for supervisors and deterministic consumers. `metrics` contains
scan/progress/idle/conflict/failure counters; no arbitrary exception text is emitted.
Unknown provider results retain their reservation and are reconciled rather than
resent. Backoff is bounded; this is not a multi-worker fairness or queue SLO claim.

## Account and authorization operations

An organization owner/admin creates invitations and workspace metadata, adds
workspace members, registers reviewed deployments and assigns use/approval grants.
Deployment workspace, application and binding cannot be moved or overwritten;
publish a new deployment identifier for a different immutable release.

Owner/admin management permissions do not implicitly grant run content or business
approval. The workbench may show administrators workspace/deployment metadata for
management while `can_use` and `can_approve` remain explicit. Ordinary members see
only current authorized directories; same-role accounts still own separate runs.
Humans have no `can_execute` permission. Revoking the final active owner is refused.
Disabling an organization also disables its public management sessions; restoring
it requires the offline identity administrator. Do not present this as a public
self-service reactivation or account-recovery workflow.

Each management mutation rechecks the original session and current account,
organization and role in its transaction. A cached Principal is not a role grant.
Organization mutation locks precede identity/authorization row locks. Cross-org
invitation acceptance locks organizations in stable order. Authentication lookup
has its own global identity privilege contract; business queries bind tenant
context transaction-locally. Pool reuse cannot retain the preceding transaction's
tenant selection. SQL services are trusted deployed code, not arbitrary SQL tenants.

Logout invalidates a session. Removing an organization member affects that
organization's sessions and execution authority without revoking other organization
memberships. Disabling an organization, workspace, member, deployment or applicable
grant prevents new protected access/admission/physical sends according to its
scope. Deployment deactivation alone preserves authorized historical reading from
PG even if its code is absent from the current registry.

The current local password adapter uses salted scrypt, digest-only invitation and
session storage, durable failed-login throttling, absolute/idle session expiry and
one-use invitations. It provides no recovery, MFA or external IdP. Invalid login
and unknown account produce the same public authentication failure. Invite secrets
are submitted in request bodies; never put them in URLs or access logs.

## HTTP contract

All private responses use `Cache-Control: no-store`. Unsafe requests require an
allowed `Origin`; authenticated writes also require the `X-CSRF-Token` returned by
login or `/v1/me`. The body schema rejects unknown trusted-identity fields.
Validation and failures are normalized: 401 authentication, 403 request
verification, 404 unavailable/unauthorized object, 409 conflict, 422 invalid input
and 500 internal error. SQL, DSNs, raw exception strings, checkpoints and full
traces are not public error payloads.

| Route | Meaning |
| --- | --- |
| `POST /v1/login`, `/v1/logout`, `/v1/invitations/accept` | Invitation adapter session lifecycle |
| `GET /v1/me`, `/v1/organizations` | Current account and active memberships |
| `POST /v1/organizations/{org}/select` | Recheck membership, revoke old session and issue an organization-bound one |
| `/v1/organizations/{org}/members` and `.../members/{account}` | Authorized member metadata and role/active changes |
| `/v1/organizations/{org}/invitations` | Create a one-use secret or list non-secret invitation metadata with pagination |
| `POST .../invitations/{invitation}/revoke` | Revoke an unconsumed invitation idempotently |
| `PUT /v1/organizations/{org}/active` | Owner-only organization state |
| `/v1/organizations/{org}/workspaces` and `.../{workspace}` | Filtered metadata and administrative workspace writes |
| `PUT .../workspaces/{workspace}/members/{account}` | Workspace membership |
| `.../workspaces/{workspace}/deployments` | Current filtered deployment directory |
| `PUT .../deployments/{deployment}` under workspace | Register/activate a server-reviewed binding |
| `PUT /v1/organizations/{org}/deployments/{deployment}/grants/{account}` | Explicit deployment use/approval permissions |
| `POST .../workspaces/{workspace}/runs` | Strict task/deployment selection plus `Idempotency-Key`; durable acceptance returns 202 |
| `GET .../runs`, `GET .../runs/{run}`, `POST .../runs/{run}/cancel` | Owner-only bounded list, status/result/usage and cancellation request |
| `GET .../workspaces/{workspace}/approvals`, `GET .../runs/{run}/approval` | Independent reviewer's exact scoped pending proposals |
| `POST .../runs/{run}/approve`, `.../reject` | Decide the exact reviewed fingerprint in an independent session |

Run and invitation pagination uses opaque object-id cursors and limits 1–100.
The Alpha's organization/member/workspace/deployment metadata directories have a
hard 100-row bound; they do not implement large-tenant administrative pagination.
The first deployment is for about 10 invited users. There is no arbitrary trusted
context, grant injection, `advance`, trace-download or SQL endpoint.

Submission freezes the organization, workspace, owner, deployment, implementation,
resource scope and input digest. Same-key retries return the accepted identity;
changing task/deployment/workspace/binding conflicts. A transactionally serialized
limit prevents unlimited admission: defaults are 10 submissions/user/minute,
30/organization/minute, 5 pending/user, 20 pending/organization and 1000 retained
runs/organization. These conservative internal limits are configurable by trusted
host code and count existing organization runs. They are not currency quotas.
The serial scanner supports at most 100 pending organization runs; configuration
above that bound is rejected rather than silently starving later work.
Do not enable unlimited real-provider traffic before monthly currency admission.

The API does not execute a model/tool. Its initial checkpoint, run binding and
outbox scheduling fact commit together; API process exit after 202 does not lose
work. Current authorization is checked again in physical admission and mark-sent
transactions. Revocation ordered after mark-sent cannot retract the network
request already entering its in-flight window. Previously sent results/fees remain
recordable/settleable even after revocation; unknown never becomes free work.

Run DTOs include only run/workspace/deployment IDs, status, cancellation request,
authorized final result, integral used/reserved units and normalized error.
`cancel_requested=true` is not proof of rollback. Reconciliation remains visible
until physical facts resolve. Approval rejection persists an immutable decision,
blocks grant consumption/new sends, requests convergence and returns
`approval_rejected` once the run reaches cancelled; existing sent facts still settle.
Competing approve/reject decisions serialize; one wins, same-decision replay is
idempotent and the opposite conflicts. Fingerprints bind the exact action, owner,
run and immutable deployment rather than the page button or a role name.
Approval grants expire after five minutes and are checked again before consumption
and physical sending. This Alpha does not replace an immutable decision with a new
reviewer's grant after expiry or reviewer revocation: cancel and submit a new run
for fresh review. The original run cannot send on the invalid grant and ultimately
converges at cancellation/deadline. This accepted liveness limit is separate from
the safety contract; production re-review/renewal is not implemented.

## Explicit upgrade and recovery

Do not rewrite `schema.sql` or migration 002 to upgrade an existing installation.
Inventory schema 2 with `control_plane_inventory`: active runs, pending approvals,
unknown calls and undelivered outbox must be understood. Stop API writers and all
workers; either drain or deliberately retain compatible legacy work under the
original trusted host. `upgrade_2_to_3(connection, writers_stopped=True)` is an
explicit transactional offline operation. Merely passing the flag is not a
distributed maintenance lock; the operator must actually stop writers.

The migration preserves every old owner, approval, invocation, receipt, checkpoint,
budget, unknown and evidence fact. It creates no human account from an old actor,
does not claim legacy runs for the platform scanner, and never dual-writes the
old SQLite reference identity. Failed DDL rolls back both version and objects.
Take a verified database backup before deployment and retain old compatible
service configuration. Downgrade DDL is not implemented; restore a verified backup
and its matching prior service version under stopped writers when rollback is
required. Do not delete unknown reservations to force a deployment upgrade.

For a queued run, check explicit worker organization, execute actor, registry
binding, scanner counters and lease expiry. For awaiting approval, use a distinct
reviewer with workspace membership and approval grant. For reconciling, restore
the trusted provider/oracle resolver and preserve all receipts; never restart by
reissuing the physical write. Errors and traces require the protected operator
channel; public HTTP intentionally does not expose internal diagnostic exceptions.

## Local qualification

`uv run --frozen python -m benchmarks.control_plane_postgres --output reports/...`
starts an owned, socket-only PostgreSQL and runs old/new PG and real HTTP tests.
`benchmarks.control_plane_verification` freezes and verifies separate candidates,
inherits all existing quality/evaluation/dependency/installation gates, reruns a
clean copy, and consumes the same wheel in independent API, worker and Chromium
processes against two synthetic applications and their external business oracle.
Browser executables/modules are explicit verification prerequisites, not runtime
dependencies. Consult its `--help` and the candidate's exact source/wheel hashes.

Production IdP/MFA/recovery, service API-key lifecycle, tenant currency allowance,
provider invoice reconciliation, multi-worker fairness/capacity, connector/secret/
data lifecycle, public deployment, real cloud backup/restore/SLO observation and
authorized real-business trials remain the S1–S7 work in the master plan.

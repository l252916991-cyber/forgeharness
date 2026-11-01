# Internal Alpha control-plane audit

Date: 2026-11-01. This records the implementation audit; qualification additionally
requires the frozen candidate's complete source and clean-copy gates. Historical
0.12.0 evidence is not used to certify changed code.

## Review coverage

Architecture: schema-3 identity is in the execution PostgreSQL authority; the
commerce SQLite identity remains compatible and independent. Web admission,
worker execution and offline migration have distinct SQL permissions. Organization
and workspace are explicit separate objects. Platform bindings are accepted with
initial checkpoints and scheduling outbox in one transaction. No new framework or
paid queue is introduced.

Correctness and concurrency: current authorization is locked in admission and
mark-sent transactions; scope/version/owner remain fixed. Unknown and sent facts
settle after revocation. Approval/rejection serialize on the run and immutable
action identity. Invocation uniqueness and lease epochs fence stale workers.
Organization-first management locking prevents deadlocks with runtime authorization;
cross-organization invitation locks are sorted. Graceful shutdown rechecks its stop
signal after scanning, before new work.

Tests: unit adapters and validators, real PostgreSQL transactions/RLS/least
privilege/migration, actual TCP HTTP, cross-process workers with abrupt faults,
external synthetic business receipts, and real Chromium UI have distinct roles.
The independent installed-wheel consumer checks actual imports, artifact hash,
two applications/organizations and physical effects. Default candidate gates retain
all legacy tests, dependency audits, 85% branch/combined coverage thresholds and
33-case framework experiment. See QUALIFICATION_REVIEW.md for evidence-validator
negative tests and strengthened fault proof.

Security: public DTOs reject client actor/roles/scope/budget/provider/import/grant
fields. All paths are checked with stored bindings; same-role owners are distinct.
Owner/admin metadata does not grant customer content or business approval.
Cookies, CSRF/Origin/Host/CORS, no-store, CSP, body bounds, normalized exceptions
and textContent rendering are tested. Tokens are digests at rest, credential reprs
are redacted, passwords salted scrypt, and authentication failure auditing omits
raw credentials. Global identity RLS additionally checks the dedicated identity
role's table privilege so an arbitrary GUC does not grant global visibility.

Dependencies: no new production dependency beyond existing optional cloud/web
extras. Browser tools are verification-only. Existing external implementation and
embedded PostgreSQL attribution is retained. Final dependency results belong to
the frozen gate output rather than this narrative.

Documentation and resume claims: ADR 046 and CLOUD_CONTROL_PLANE.md describe
current contracts, explicit migration and operator-stopped-writer requirement,
history compatibility, reconciliation and remaining S1–S7 scope. No downgrade,
cloud HA, public registration, monthly CNY allowance or production IdP is claimed.
The pause/kill/recover consumer verifies fencing against actual process faults;
normal process exit alone is not treated as crash recovery evidence.

## Classified findings

| ID | Severity | Finding | Disposition / regression |
| --- | --- | --- | --- |
| CP-01 | high | Runtime authorization covered legacy owner-active but not workspace/deployment/grant | Fixed in accept/admit/mark-sent; queued, model-result, unsent-admission and scoped-approver PG regressions |
| CP-02 | high | Approve/reject race had no durable single decision contract | Immutable decision table and run-lock serialization; precise fingerprint, replay/opposite conflict and worker-convergence tests |
| CP-03 | high | Different worker PIDs alone did not prove interruption recovery | Installed consumer now SIGSTOP/SIGCONT and SIGKILL after oracle commit, epoch takeover and late-old-worker fencing; no duplicate dispatch/effect |
| CP-04 | medium | Organization/shared-lock upgrade and cross-organization invitation ordering could deadlock | Organization-first manager/session locks and sorted cross-org locks; concurrent PG ownership/invitation regressions |
| CP-05 | medium | A global identity GUC alone could expand account metadata visibility | Dedicated-role privilege predicate plus RLS; runtime GUC-spoof regression |
| CP-06 | medium | Prior decision could hide all later proposals in a run | Queue filters outstanding unconsumed approvals; consumed-prior-action regression |
| CP-07 | medium | Shutdown arriving during a scan could still start work | Stop recheck before advance; real-PG zero-new-epoch regression |
| CP-08 | medium | Configured pending count above scanner bound could starve later runs | Reject org_pending >100; capacity configuration regression |
| CP-09 | medium | Authentication outcome history absent; invalid Unicode could escape normalization | Append-only global authentication audit, normalized login/invitation failure, no-secret and SQL privilege regressions |
| CP-10 | low | Workbench password maximum differed from adapter | Unified to 1024 and adapter/UI tests |
| CP-11 | low | Verification HTTPS wording and browser binary lookup overstated local setup | Corrected TLS-host boundary; explicit browser executable with no skipped browser fallback |
| CP-12 | high | A joined checkpoint snapshot taken before waiting for the run lock could accept a superseded proposal fingerprint | Confirmed on frozen r1; fixed by locking run first and reading checkpoint in a fresh statement. Two real lock-wait regressions and the full r4 source/clean suites pass |
| CP-13 | medium, verification | Browser helper required the task section for an approval-only account, conflicting with correct capability-based UI visibility | r2 retained as failed (128/130); fixed capability-specific waits. r4 same-wheel browser proves reviewer submission hidden and review visible |
| CP-14 | medium, evidence coverage | Upgrade tests did not populate old checkpoint, reserved usage and evidence-chain tables when comparing schema-2 facts | Fixed by an additional self-contained consumer with hashed protocol. Exact r4 wheel audit populates all 11 legacy fact tables and passes rollback/upgrade/grant refresh/full row and inspect/trace byte comparison |
| CP-15 | high | Login/invitation forms without explicit POST leak password/token into the URL when JavaScript is unavailable | Confirmed with Chromium on unchanged r3 despite 130 passing gates; r3 audit-rejected. Fixed explicit POST/action. r4 same-wheel disabled-JS and blocked-script scenarios pass with no URL/error secrets, and negative evidence tests pass |
| CP-16 | medium | Artifact staging checked a passing gate summary but did not enforce a later persistent audit rejection | Stage now refuses any REJECTED.json/ABORTED.json entry, including broken links, before artifact promotion. Four regression cases pass; included in r4 freeze so rejection is enforced by the tool, not conversation text |
| CP-R1 | medium, accepted | An expired/revoked approval grant cannot be renewed/reassigned on its immutable decision | Fail closed. Cancel and submit a new run for fresh review; original run converges at cancel/deadline. Re-review/renewal is later product work. UI/guide disclose this |
| CP-R2 | low, accepted | Management metadata directories cap at 100 and terminal pages keep polling | Internal ~10-user scope; run/invitation lists have pagination. Large-tenant management and polling optimization remain later |
| CP-R3 | medium, accepted deployment constraint | Stopping writers for upgrade and reactivating a disabled organization require offline operator control | Explicit upgrade flag is acknowledgement, not distributed maintenance fencing; backup/restore downgrade and live upgrades are not claimed |

No known blocker/high finding is accepted. New confirmed defects discovered in
candidate verification must be classified and fixed in a fresh candidate, keeping
failed candidate evidence. A clean run of all gates is required before completion.

## Final disposition — 2026-11-02

Accept candidate-r4 for invitation-only internal Alpha engineering qualification.
Source identity is `05bf6eaa6817100d22e1ddac760a05547a78c693a09fb1ceeb1eac7d69e6b984`.
All 130 complete gates pass; source and clean copy each pass 2050 core and 234
combined PostgreSQL/real-HTTP tests. The same built wheel passes full Chromium,
two script-unavailable secret-protection modes, independent approval, actual
SIGSTOP/SIGCONT and SIGKILL recovery, and the additional installed migration audit.
Known open blocker/high count is zero. Medium/low residual boundaries above are
accepted only for this declared internal Alpha scope. Artifacts are staged locally;
production SaaS, real deployment and real business remain unqualified.

The original r1/r2/r3 manifests and results remain intact. A later audit rejection
overrides an older passing gate summary in the staging tool. Machine-readable final
decision: [AUDIT_ACCEPTED.json](candidate-r4/AUDIT_ACCEPTED.json).

## Independent reviewers

Identity implementer cross-reviewed HTTP/assets, API implementer reviewed execution
authorization/worker semantics, and qualification implementer reviewed protocol,
installation and evidence validators. Root reviewed integration, legacy boundaries,
documentation and the combined candidate. These are engineering reviews within
the multi-agent team, not an external security certification or customer audit.

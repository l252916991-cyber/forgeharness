# Qualification and recovery evidence review

Date: 2026-11-01. This is a pre-freeze independent review, not a qualification
decision. The final candidate report must bind the reviewed source, protocol,
complete gate results and the same wheel consumed by external processes.

The new driver subclasses `ComparisonVerification` and invokes every inherited
quality, evaluation, dependency, archive and installation gate. It adds the
control-plane PostgreSQL suite before coverage is graded and adds an independently
installed HTTP/worker/Chromium consumer of the same built wheel. The existing
183-test PostgreSQL floor, 85% pure-branch/combined thresholds, safety selection,
33-result comparison oracle and dependency advisory policy are preserved.
New tests are collected by their actual executed names; empty, skipped or failed
control-plane suites cannot be relabeled as successful legacy verification.

The 0.13.0 experiment declares a new protocol and predecessor hash. Existing
protocol bytes and 0.12.0 candidate evidence are retained. The same comparison
engines, nine scenarios, budgets and grading remain in force; neither framework
superiority nor customer-business qualification is asserted. The content snapshot
includes source, tests, docs, benchmarks, evals, examples, deploy/data and selected
root metadata. Reports and generated Python caches do not feed back into its hash.

The installed consumer rejects source/editable production imports before creating
any PostgreSQL server. API acceptance is observed over real loopback HTTP, then
the API process exits. Independent worker processes discover durable work, and a
new API process observes the saved result. Two application fixtures use a separate
business ledger whose effect counts, full provider receipts and physical dispatch
counts are checked against PostgreSQL usage and outcomes.

Recovery is tested at a committed business effect whose runtime receipt has not
arrived. One worker is suspended with SIGSTOP, a new epoch reconciles the existing
effect, and the old worker resumes with SIGCONT. Another worker is killed with
SIGKILL and replaced. Both paths retain the sent reservation until reconciliation,
increase the epoch, settle the actual cost, preserve the final PostgreSQL facts
after old-worker resumption and leave exactly one business effect/physical send.
Returning an already-final view to an old worker is allowed; its own reported
progress counter is not evidence that it wrote the new epoch.

Chromium drives the shipped page through administrator invitation, member
acceptance/login, workspace/deployment grants, task submission, independent
reviewer approval, result/usage display, queued cancellation, revocation and logout.
The page reaches the same live HTTP/PG services as the API tests. Browser traffic
is restricted to the owned loopback origin. Node/Playwright/browser are explicit
verification prerequisites, not package runtime dependencies. The concrete
executables and Chromium version are retained with the final attempt evidence.

## Findings classified before freeze

| ID | Severity | Finding and disposition |
| --- | --- | --- |
| CP-Q01 | High, closed | Different worker PIDs alone did not substantiate crash recovery. Replaced with committed-effect SIGSTOP/SIGCONT and SIGKILL takeovers, epoch/cost/oracle checks, and fail-closed recovery-evidence validation. |
| CP-Q02 | Medium, closed | A boolean-only consumer summary could omit browser/fault details. The gate now requires all named boundaries, actual browser/version evidence, six distinct process IDs, both fault modes and increasing epochs; negative regressions reject malformed/incomplete evidence. |
| CP-Q03 | Low, closed | A consumer comment implied built-in HTTPS enforcement. Corrected to Secure-cookie defaults and separately configured TLS termination, consistent with the deployment guide. |
| CP-Q04 | Low, closed | Local Playwright expects a browser revision absent on this machine. Qualification accepts an explicit installed Chromium executable and records tooling instead of skipping the browser gate. |

No blocker/high qualification finding is accepted as a residual. These closure
claims still require the final unchanged candidate to pass the entire suite.
Development runs, including a successful installed-wheel browser/fault consumer,
are diagnostic evidence and do not certify later source edits.

The first frozen attempt, candidate-r1 (`c087c7aeabb3662624feb01f092f274bf98e29f7f382382b481a903a0f9349c8`),
was interrupted after independent real-PG review confirmed product finding CP-12:
one lock-waiting join could return a current run and an earlier checkpoint,
allowing a stale reviewed fingerprint. The generated summary remains unqualified;
`candidate-r1/ABORTED.json` records SIGINT and process cleanup. Its 2036 passing
core tests and 24 completed checks do not establish completeness. The probe and
failure evidence are preserved. A corrected candidate must rerun all gates from
a new source freeze; no passed result is carried forward. The unchanged
SDK0130-r1 comparison protocol remains appropriate because its engines, scenarios,
budgets and grading were not changed by the product defect fix.

Candidate-r2 freezes the correction as
`800de184015ff9d4f019945d8c1422040994655d7f7f40a2c79f634f16ee45cb`.
The corrected read first locks the run and then obtains its joined checkpoint in
a new statement. `evals/saas_postgres/tests/test_control_snapshot.py` contains
the real lock-wait regressions for approval decisions and owner inspection. The
focused two-test PostgreSQL run passed before freeze; final closure still depends
on the new complete source/clean verification and same-wheel consumer.

On 2026-11-02, candidate-r2 was found to have completed 130 gates with 128 passes
and two failures, both from its installed control-plane consumer. All original
source/clean quality, PostgreSQL, dependency and archive gates passed; the complete
candidate remains unqualified. CP-Q05 is a medium verification defect: the browser
helper always waited for a submission form, although the reviewed product UI
correctly hides that form from an approval-only reviewer. The original failure
and wheel are retained. The helper now checks the expected current capabilities
without granting the reviewer submission access or increasing a timeout. The
browser reports and gate validates explicit can-submit=false/can-approve=true
evidence; negative regressions reject missing, broadened and malformed evidence.

The consumer also strengthens the required self-approval scenario: its owner is
given genuine current use and approval grants, then its independent session is
still refused at review, approve and reject. PostgreSQL must contain no resulting
decision/approval before the separate reviewer approves the exact fingerprint.
The corrected consumer passed diagnostically against candidate-r2's installed
wheel, isolating the verification error from product behavior. That diagnostic
does not qualify r2 or any new source; candidate-r3 must freeze and rerun every
source/clean and same-wheel gate in a single complete attempt.

Candidate-r3 subsequently passed all 130 gates, but was rejected by audit finding
CP-15 (high): without JavaScript, the native credential forms used GET and put
password/invitation secrets into URLs. The real Chromium probe is retained under
`cp15-nojs-probe/`; `candidate-r3/REJECTED.json` rejects the candidate without
altering its generated passing summary. Markup now explicitly uses same-origin
POST for both forms. The installed-wheel browser consumer adds disabled-JavaScript
and blocked-script cases for both forms, verifies POST and secret-free request/page
URLs and error bodies, and requires normalized safe rejection (403 Origin refusal
or 422 JSON-body refusal). Ordinary JavaScript flows retain their exact success
assertions. No Origin/CSRF rule or normal reviewer permission was weakened.

The first development diagnostic expected only 422 and correctly retained a
failure when the native Origin guard returned 403. The declared fail-closed
contract above accepts both specific normalized security outcomes. The complete
corrected development consumer then passed against a newly built wheel; this is
pre-freeze diagnostic evidence only. Candidate-r4 must run the entire suite again.
Release staging now rejects any persistent REJECTED.json or ABORTED.json entry,
including a broken symlink, so audit rejection is enforced outside conversation
text even if an earlier gate summary says qualified=true.

## Candidate-r4 gate outcome (2026-11-02)

The complete independently frozen attempt
`candidate-r4/verification/runs/20261101T165326Z-e2129352` passed 130/130 gates.
The source identity remains
`05bf6eaa6817100d22e1ddac760a05547a78c693a09fb1ceeb1eac7d69e6b984`.
Source and clean copies each passed 2050 core tests, the 227-test PostgreSQL suite
and the 234-test PostgreSQL/HTTP combination (the latter includes the former;
these are separate gate runs, not distinct-test counts to add). No test failed,
errored or skipped. Both profiles recorded 91.8081424% combined coverage and
85.3434817% pure branches without changing the 85% thresholds.

The new artifact build and installed consumer passed all 14 boundary checks.
Chromium 156.0.8078.4 completed the normal UI and disabled-JavaScript/blocked-script
native secret-transport regressions. Approval-only permissions, dual-role owner's
self-decision refusal and both committed-effect process fault modes passed.
The consumer loaded its production modules from its installed artifact, used no
external model/business service and stopped its owned PostgreSQL. Its wheel SHA
is `31017b4afde8c1c547bd62129d5019d054101cdc54f70f0990534ca1bdb434a7`.
The separately bound source archive SHA is
`11deabd0f92721c19db2dbe2cdde5eefb928f232f656ba4b0f5acfcab690dacc`.

This records the completed gate outcome; final audit disposition and the additional
same-wheel migration audit belong to the final report. It does not override the
rejected earlier candidates or claim complete production SaaS qualification.

## Accepted boundary and residual work

The scope is an invitation-only, serial internal Alpha. Browser tests use explicit
loopback HTTP with Secure cookies disabled for that local adapter; this is not a
TLS/reverse-proxy acceptance test. Production IdP/MFA/recovery, global ingress/IP
abuse protection, machine credential lifecycle, CNY accounting, multi-worker
fairness and capacity, cloud backup/restore/SLO observation and real customer data
remain subsequent qualifications. All provider/business records here are synthetic;
the independent ledger's SQLite is an external oracle, never platform state.

The upgrade test consumes the shipped schema1/2/3 resources, inventories active
legacy/unknown work, injects an outer transaction failure and verifies rollback,
then compares legacy owner/approval/invocation/receipt/outbox facts after upgrade.
The broader PostgreSQL migration suite must also retain checkpoint/evidence/budget
history and fresh-install coverage. The maintenance flag is an operator assertion,
not a distributed writer lock; downgrade DDL is absent. Those limitations are
explicit in `docs/CLOUD_CONTROL_PLANE.md` and cannot be reported as proven cloud
recovery or a completed production SaaS.

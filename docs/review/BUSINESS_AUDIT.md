# 0.5.0 synthetic candidate audit scope

Date: 2026-10-16. Candidate identity and final qualification are recorded in
`reports/business-release-audit.json`; no earlier report certifies this tree.

## Reviewed corrections

| Finding | Classification | Correction and regression |
| --- | --- | --- |
| STATE-001 tool sources absent from session dependencies | high | Governed receipts and durable evidence are rechecked on history/inspect/events/resume; source withdrawal regression |
| STATE-002 failed/no-checkpoint cleanup could not be retried | medium | Binding-safe allow-missing tombstones and repeated session/workflow cleanup |
| STATE-003 recursive repeated history checks | medium | Iterative traversal, shared per-operation reads, 512-turn refusal bound, oldest-dependency regression |
| BUS-001 provider await reused stale permission conclusions | high | Final context-provider barrier rechecks current materials, ancestors and READ authorization before any model call |
| BUS-002 staging identity and sweep crossed tenant/actor boundaries | high | Scope-bound identities/files, per-material authorization, active-task protection independent of caller listing |
| BUS-003 parser output changed after cached validation | high | Draft parses once and validates that same object with the current validator |
| BUS-004 task results retained deleted material text | high | Import task results contain only resource/version/digest/state references |
| BUS-005 missing task ledger allowed cleanup of active imports | high | Linked imports require task evidence; cleanup fails closed without it |
| BUS-006 deletion reused task identity; crash requeue exceeded budget | high | Persistent task/business-key tombstones and exhausted-budget refusal |
| BUS-007 dynamic retrieval authorization followed scoring | high | Verify current access before scoring and again before returning hits |
| BUS-008 customer knowledge-management reads exposed drafts | high | Staff-only management endpoint; consultation reads published records |
| BUS-009 same actor could approve own commerce write | high | Distinct actor required for supervisor approval |
| BUS-010 live product version could name changed contents | high | Ledger seed rejects same-version mutation and version rollback |
| BUS-011 legal adapter lacked page confinement and exact current page matching | high | Per-page grants, local path confinement, duplicate-locator refusal, whole-page comparison |
| BUS-012 completed handoff alias failed final verification | medium | Both registered handoff names verify the completed ledger receipt |

Architecture reviewed: framework-independent core, public SDK-only application imports,
optional FastAPI modules, no core import of business or comparison frameworks.
Persistence reviewed: scope bindings, stable keys, transaction/lock boundaries, tombstones,
source lifecycle and honest cancellation/reconciliation. Tool results are validated;
source readers must explicitly declare `governed_sources`. Compatibility default false
is not a guarantee that arbitrary host tools have declared all their dependencies.

The source suite, targeted regressions, dependency audits, default keyless controls,
120 development contracts, selected safety nodes, package byte verification and
minimal install run again from a frozen independent copy. Final measured values belong
in the report, which is outside the source snapshot and can be finalized without
changing the candidate. Passing deterministic tests is not a business-quality result.

## Accepted limitations of this candidate

- Local POSIX/SQLite only; no distributed leases, queue/outbox or production HA.
- Dynamic permission callbacks and adapters are trusted host integration points.
- Histories exceeding 512 reachable turns refuse exhaustive validation; event pagination
  still reads/verifies the whole local trace. No high-volume latency target is claimed.
- Scope-bound staging avoids tenant cleanup collisions; global orphan-file maintenance
  is left to the host. Deletion is not secure erasure of host backups or external logs.
- Keyless fixture models, synthetic pages and catalogue; exact spans prove provenance,
  not legal or commercial reasoning. No real LexVault same-question comparison exists.
- No-product questions return a human-help indication; a product selection is needed
  for the current product-ticket tool. General tickets and natural-language clarification
  remain planned. Existing ticket APIs are host-level ledgers, not authorization services.
- The synthetic sealed fixture has not been evaluated and lacks independent professional
  authorship. Real-model grading, targets, reviewer and authorized business corpus remain
  prerequisites for business qualification.
- Optional all-extras audit exception PYSEC-2026-3740 retains the prior documented NLTK
  risk; core must have no accepted vulnerabilities. New advisories are not auto-accepted.
- Legacy knowledge platform findings FH-015–FH-018 are not closed by this candidate.

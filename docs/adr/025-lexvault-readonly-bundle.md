# ADR 025: LexVault-shaped read-only bundle boundary (F2a)

Date: 2026-10-18. Status: accepted for implementation.

## Need

F1b proved that a custom `Orchestrator` can use the governed `RunKernel`, but only
with a synthetic two-node DAG. F2 needs a legal-shaped workload. Connecting the live
LexVault SQLite database and UI first would mix framework validation with host
integration, schema migration and write-path concerns.

F2a therefore freezes the smallest read-only host boundary: a case bundle of page
text plus case metadata. It can later be produced by a LexVault exporter or replaced
by a live read-only adapter without changing the DAG.

## Decision

Add `forgeharness.applications.legal.lexvault`:

* `CaseBundle` / `BundlePage`: case metadata and page text, bounded, unique page
  numbers, no database.
* `BundleCaseAdapter`: implements `CaseAdapter`; every read checks tenant, case and
  page resources from `RunContext`. It performs literal page search only; it is not
  LexVault's FTS5/vector/RRF retrieval.
* `LexVaultDagOrchestrator`: runs `facts`, `evidence` and `contradiction`
  deterministic specialists with `asyncio.gather`, emits a trace event per node,
  then makes one `kernel.call_model(phase="critic")` call and finishes through the
  kernel.
* `BundleCitationVerifier`: accepts `[page:N]` only when N exists in the bundle; an
  explicit insufficiency statement may omit citations. It does not assess semantic
  support or legal correctness.
* `lexvault_dag_application`: composes the app through `Capability` +
  `compose_application` with a read-only policy.

## Boundaries

* Synthetic/bundle only. It does not read `law_review.db`, does not export from
  LexVault, and writes nothing.
* Literal search is a test fixture, not a retrieval-quality claim.
* The DAG specialists are simple lexical scanners; they are not LexVault's
  production experts and carry no legal-quality claim.
* No write tools, approval, journal recovery or live UI.
* `BundleCitationVerifier` proves page identity only; a lawyer must review.
* F2b is the live read-only host adapter and same-question comparison; it may replace
  `BundleCaseAdapter` while keeping `LexVaultDagOrchestrator`.

## Verification

`tests/unit/test_lexvault_dag.py` covers the parallel DAG + Critic success path,
invalid page citation, uncited factual answer, explicit insufficiency, duplicate
pages, tenant/page scope, and exact-span search. The existing `RunKernel`,
capability, legal-pilot and import-boundary suites remain the regression gate.

## Consequences

* The framework now has a legal-shaped DAG that uses `RunKernel`, trace, budget,
  checkpoint and verifier without touching LexVault.
* The next step is an exporter/live adapter with the same bundle contract, not a
  rewrite of the DAG.
* The verifier is intentionally weaker than the citation machinery in
  `applications/legal/pilot.py`; do not present it as equivalent.

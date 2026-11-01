# ADR 027: bundle citation integrity and export snapshots

Date: 2026-10-20. Status: accepted for implementation. Supersedes the citation
clause of ADR 025 (page-identity-only checking) and corrects the shim inventory
in ADR 024.

## Need

The 2026-10-20 review found three integrity gaps in the LexVault read-only path:

1. `BundleCitationVerifier` only proved that `[page:N]` exists. A page's text could
   change (or a bundle be tampered with) and citations still passed; there was no
   per-page digest and no exact-quote check.
2. `bundle_from_lexvault_db` ran two independent SELECTs without a read transaction,
   stripped page text (destroying exact-offset fidelity), and derived `version`
   solely from `cases.updated_at` — an edit that forgets to bump the timestamp
   produced an identical version for different content.
3. `tests/unit/test_import_boundaries.py` scanned only static `import` statements,
   so a new `import_module("forgeharness.applications…")` in core would slip past.
   Enabling the dynamic scan immediately found a third, previously undeclared
   Coding shim: `context/repository.py`.

## Decision

### Page and bundle digests

* `BundlePage.text_sha256` is mandatory. It is auto-derived from `text` when
  absent, and when supplied it must equal `sha256(text)` — a snapshot cannot be
  re-labelled onto different content.
* `CaseBundle.snapshot_digest` is a canonical content-addressed digest over
  tenant/case/version and every page's locator, number and text digest.

### Citation contract

* `[page:N]「exact quote」` — the quote must be a verbatim substring of the page's
  raw text; empty or paraphrased quotes fail.
* `BundleCitationVerifier(strict_quotes=True)` requires a quote block after every
  marker. Default mode still accepts bare `[page:N]` for compatibility.
* Verification recomputes each cited page's digest, so a mutated bundle object
  fails closed.
* The explicit "材料不足" refusal path is unchanged. Semantic support and legal
  correctness remain unassessed.

### Export snapshot

* The exporter reads the case row and its pages inside one explicit `BEGIN`…`COMMIT`
  read transaction (`isolation_level=None`, `mode=ro`, `query_only`).
* Page text is stored verbatim; `strip()` only decides whether a page is skipped.
* Default `version` is `{case_id}:{updated_at}:{pages_digest16}`, so any page-text
  change moves the version even when `updated_at` does not.

### Boundary scan

* `_imported_modules` now also collects string-literal arguments of
  `import_module(...)`/`__import__(...)` calls.
* The frozen API-v1 shim allowlist is corrected to **three** files:
  `runtime/verification.py`, `context/__init__.py`, `context/repository.py`
  (all lazy Coding re-exports, removed at API v2). ADR 024's text said two; the
  third existed but was invisible to the static-only scan.

## Boundaries

* Digests prove content identity, not semantic support; a lawyer must still review.
* The read transaction gives a consistent snapshot, not serialization against a
  live writer; the source database is still opened read-only and never written.
* Quote syntax `「…」` is a bundle-verifier contract, not an SDK-wide citation
  format; the `RevisionStore`/`CitationVerifier` path is unchanged.

## Verification

`tests/unit/test_lexvault_dag.py` adds exact-quote pass, paraphrase rejection,
strict mode, empty-quote rejection, digest-tamper detection, digest
derive/enforce and snapshot-digest drift. `tests/unit/test_lexvault_export.py`
adds raw-text preservation (with whitespace-only skip) and version/digest drift
after a page edit without `updated_at` change. `test_import_boundaries.py` adds
dynamic-import detection and the corrected allowlist. Full suite, Ruff and mypy
must stay green.

# ADR 017: synthetic business contracts and one-shot sealed governance

Date: 2026-10-15. Status: implemented for a local synthetic candidate.

## Decision

Development input is `evals/business/development.json`: 40 legal and 80 commerce
contract scenarios, grouped by case/product/FAQ family. These are not 120 independent
business questions. The legal cases share two synthetic matters. Default reports
force `measurement_valid=false`, `business_qualified=false`, null semantic correctness,
null human review and null real token accounting. Latencies include fixture setup;
no service or model performance claim follows. Real-model grading remains future work.

Safety nodes are listed separately in `evals/business/safety.json` (at least 40).
They are actual pytest regression nodes, including the control evaluation that tests
untrusted-content injection; the release gate runs them and also runs the full suite.

The 60 synthetic sealed fixtures reside in a separate file under `data/sealed/`.
Only count, disjoint families and a hash are used during development. They are machine
authored fixtures, not independently authored professional questions, and have not
been used for evaluation. They are excluded from the public sdist. A real business
claim still requires independently curated material and reviewers.

`benchmarks.business_seal.freeze_targets` creates an exclusive immutable declaration
binding source, model, data hash, scoring version, reviewer, thresholds and abort
conditions. `open_sealed` verifies the prior declaration and exact bytes, then
exclusively writes and fsyncs a consumption marker **before JSON decoding or running**.
A crash, invalid JSON or failed evaluation still consumes the set. A second opening
is refused even with a new target file for the same data hash. Host-controlled marker
storage is part of the trusted audit boundary; this is not a remote tamper-proof seal.

Business targets are not inferred from keyless pass rates. The proposed template is
in `evals/business/quality-targets.json`; no professional result or reviewer is
fabricated. The real-model adapter, grading and independent legal review are outside
this simulated qualification.

## Verification

`tests/unit/test_business_evaluation.py` covers set identity/count/family metadata,
truthful report flags, missing/future targets, changed sealed data, exclusive freezes,
and consume-before-decode. It uses separate tiny test data, never the sealed body.
`benchmarks/business_verification.py` binds all release evidence to one frozen source,
repeats gates from an independent clean copy, and verifies public package contents.

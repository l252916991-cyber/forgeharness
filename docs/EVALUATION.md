# Evaluation and claim policy

## Reproducible suites

| Command | Purpose | Current checked-in evidence |
| --- | --- | --- |
| `forge eval-control` | Seven deterministic Harness control/failure cases | `reports/control-eval.json` |
| `forge eval-rag` | Fresh-index 60-case retrieval/citation/refusal gate | `reports/rag-eval.json` |
| `forge eval-reviewer` | Same-data baseline/Reviewer quality and latency enablement decision | `reports/reviewer-experiment.json` |
| `forge bench-retrieval` | 5,000 chunks, 10 warmups, 100 requests, concurrency 10 | `reports/retrieval-benchmark.json` |
| `forge qualify-omlx --quick` | Two real cases for each OMLX capability | `reports/omlx-qualification-quick.json` |
| `forge qualify-omlx` | 20 tool + 20 intent + 20 vision + 10 embedding + 30 reranker cases | `reports/omlx-qualification.json` |
| `python benchmarks/platform_verification.py` | Live two-API, Postgres, Redis/ARQ, Qdrant, Nginx recovery/failover drill | `reports/platform-verification.json` |
| `python benchmarks/freeze_candidate.py` | Exact tracked and unignored file hashes for an uncommitted candidate | `reports/candidate-manifest.json` |
| `python benchmarks/verify_clean_candidate.py` | Fresh locked Python 3.12 source-copy verification, without reusing `.venv` | `reports/clean-verification.json` |
| `python benchmarks/check_branch_coverage.py` | Pure branch gate, separate from statement/branch combined coverage | `reports/coverage.json` |

Every result report includes the visible Git revision and per-case or workload configuration. Because this candidate contains user-owned uncommitted changes, the final audit additionally verifies every tracked and unignored candidate file against `candidate-manifest.json`; result and review directories are excluded to avoid a circular hash.

## Gates

- Hybrid Recall@5 >= 0.85.
- Pure branch coverage >= 85%; `coverage.py` combined coverage is reported separately and cannot substitute for this gate.
- Citation precision >= 0.90.
- Unsupported-answer rate <= 0.10.
- Reranking MRR@10 must not fall below fused MRR@10.
- Deterministic warmed retrieval p95 < 500ms at the declared workload.
- Full OMLX tool-call and intent pass rate >= 0.90, vision >= 0.85, embedding >= 0.90 and reranker >= 0.80.
- Reviewer may default on only if answer quality improves by at least 0.05 and median latency is no more than 2x the single-agent path.

## Current measured results

- RAG control set: 60 cases, Recall@5 1.000, MRR@10 1.000, citation precision 1.000, unsupported-answer rate 0.000.
- Local deterministic benchmark: 5,000 chunks, 100 measured requests, concurrency 10; the latest report passed the <500ms retrieval p95 gate. Use the report's measured value rather than an older faster run.
- Full OMLX qualification: tool 20/20, intent 20/20, vision 20/20, embedding 10/10, reranker 30/30. The final full run's median latencies were about 2.49s, 0.91s, 42.97s, 0.21s, and 0.15s. A preceding long-running attempt was interrupted and OMLX restarted; cold/loaded-server behavior varies materially.
- Reviewer experiment: both arms 60/60 on the deterministic control set, 0 percentage-point improvement; Reviewer remains default-off. Use the latest report for latency; fake-model timing differences are not a speedup claim.
- Live platform drill: 30/30 queued jobs succeeded, the additional outage-time job recovered, and 20/20 requests succeeded after one API process stopped. Use `platform-verification.json` for the current readiness/upload percentiles.
- Locust's saved CSV snapshot records 1,821 requests and zero failures with 10 simulated users, configured 30-second duration, a single keyless API and an initially empty, growing fixture corpus. It is not a 5,000-chunk/Qdrant/OMLX throughput result. Console shutdown totals may exceed the final periodic CSV snapshot; use the saved artifact's denominator.
- Runtime/platform/development dependency audit: zero known vulnerabilities at audit time. The optional all-extras environment has one accepted no-fix NLTK advisory through LlamaIndex and is not the runtime profile.

These values may be quoted only with the workload qualifier. They are not production or business-task success rates.

## Final release gate

`forge review` rejects missing, undersized, revision-mismatched, or failed reports; unresolved blocker/high findings; a stale source snapshot; insufficient pure branch coverage; or an audit decision other than `passed`. A source snapshot cannot retroactively prove that an older real-model report executed that exact source. The current full-plan audit is held open until these provenance and scope gaps are resolved. See `docs/STATUS.md` and `docs/review/AUDIT.md`.

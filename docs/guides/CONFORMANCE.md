# Application Conformance

Current index (2026-10-26): [capabilities and host bindings](../CURRENT_CAPABILITIES.md).
R2 explicit graphs implement write/approval/resume in SDK 0.8.3; the arbitrary
one-shot kernel remains read-only. The reference-app table below describes each
app's selected path, not a framework-wide absence of R2. Real host task/HTTP checks
are separately indexed in `reports/improvement-20261026/REPORT.md`.

This is the application-level contract for ForgeHarness reference apps. It is not a
claim that legal/commercial output is correct. It has two layers: the application
contract (F3a) and a shared runtime matrix subset (F3b); it is not the full
scope/policy/approval/budget/trace matrix.

## Current coverage

| Check | Legal pilot | Legal DAG | Commerce | Commerce readonly |
|---|---|---|---|---|
| `ApplicationSpec` identity/version/policy | yes | yes | yes | yes |
| Tool names unique; tools executable | yes | n/a (no tools) | yes | yes |
| Tool order stable | `search_case` → `read_case_page` | n/a | 5 fixed tools | `product_lookup` |
| Policy/verifier present | yes | yes | yes | yes |
| Wrong application binding rejected before run | yes | yes | yes | yes |
| Core import boundary | `tests/unit/test_import_boundaries.py` | same | same | same |
| Composition via `Capability` | yes | yes | yes | yes |
| Custom orchestrator read-only DAG | n/a | `LexVaultDagOrchestrator` | n/a | n/a |
| Write/approval custom path | no (ReAct-only, ADR 026) | no | no | no |

## Shared runtime matrix (F3b)

`tests/unit/test_runtime_conformance.py` runs the same subset for legal DAG and
commerce:

| Runtime behavior | Legal DAG | Commerce |
|---|---|---|
| Successful run | `[page:1]` verifier | `product_lookup` + `CommerceVerifier` |
| Trace hash chain | yes | yes |
| `inspect` after run | yes | yes |
| Invalid final rejected by verifier | yes | yes |
| Wrong application binding rejected | yes | yes |
| Missing resource fails closed | no page grant → failed | no `product:a` → policy deny → failed |

This matrix intentionally does not force the apps to have the same tools or
capabilities; it checks shared runtime behavior.

## Tests

```bash
uv run --locked pytest tests/unit/test_application_conformance.py -q
uv run --locked pytest tests/unit/test_runtime_conformance.py -q
uv run --locked pytest tests/unit/test_import_boundaries.py -q
uv run --locked pytest tests/unit/test_lexvault_dag.py tests/unit/test_lexvault_export.py -q
```

## What is not covered yet

- The full runtime matrix: approval/resume, budget exhaustion, policy permutations and
  every tool failure class are covered by separate tests, not one shared table.
- R2 application-specific acceptance beyond the shared reference-app table;
  SDK write/approval/journal recovery is implemented for explicit R2 graphs.
- Real model quality, lawyer review, live LexVault UI/ARQ.
- Optional-extras packaging/install conformance.
- Semantic citation support/legal conclusions; citation checks prove identity only.

## Adding a new application

1. Compose through `capabilities=(Capability(...),)` + `compose_application`.
2. Keep domain tools/policies/verifiers in the application package; never import them
   from core or another application.
3. Add the app to `tests/unit/test_application_conformance.py` with its tool order.
4. Add an import-boundary check if the app adds a core dependency.
5. For custom writes use an explicit `DurableOrchestrator` with current resource
   validation and authoritative reconciliation. Arbitrary one-shot kernels stay read-only.

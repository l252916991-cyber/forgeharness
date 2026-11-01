# ADR 024: core/application dependency boundary

Date: 2026-10-18. Status: accepted for implementation.

## Need

The framework now has `Capability` composition and an application-provided
`Orchestrator`/`RunKernel`, but the boundary was still implicit: core modules carried
Coding compatibility shims, `ARCHITECTURE.md` described Coding as the default shape,
and no reference application used the new composition API. Without an executable
boundary, the next application would keep adding domain code to the core.

## Decision

Adopt this dependency direction:

```text
applications / host integrations
        |
        v
       sdk
        |
        v
runtime / domain / tools / context / state / observability
```

* Core packages are `domain`, `models`, `runtime`, `tools`, `context`, `state`,
  `observability`, `sdk`. They must not import `forgeharness.applications`.
* Core must not import `forgeharness.coding` except three API-v1 compatibility shims (the third was identified by the dynamic-import scan in ADR 027):
  - `runtime/verification.py` lazily exposes `CodingVerifier`;
  - `context/__init__.py` lazily exposes `PythonRepositoryMap`;
  - `context/repository.py` forwards Coding repository types.
  The separate `sdk/application.py` re-export shim preserves SDK imports; it is not a Coding dependency exception.
  `runtime/__init__.py` forwards the former through the same shim.
* `tests/unit/test_import_boundaries.py` performs an AST scan for imports and a clean
  subprocess import. New core -> application/coding imports fail the test.
* `applications/legal` now builds its tools through `Capability` +
  `compose_application`; tool order and behavior remain unchanged.
* `ARCHITECTURE.md` presents the SDK composition root and the two execution paths
  (ReAct and application orchestrator) instead of Coding as the default.

The shims remain because SDK API v1 is already public; they are removed at API v2.
The boundary test intentionally allowlists them so the debt is visible rather than
silently growing.

## Boundaries

* No new core -> application import, including type-only imports.
* Coding and legal are reference applications, not framework defaults.
* Commerce migration to `compose_application` is deferred; one real adoption plus a
  boundary test is enough to establish the rule.
* This ADR does not add write/approval support to custom orchestrators (F1c), and
  does not integrate LexVault (F2).
* Host integrations remain outside core; they may import `sdk` but core must not
  import them.

## Verification

* `tests/unit/test_import_boundaries.py` checks no application import, only the two
  declared coding shims, and that importing all core packages in a clean process does
  not load `forgeharness.applications` or `forgeharness.coding`.
* Existing legal pilot tests pass; one assertion checks tool order
  `search_case`, `read_case_page` after capability composition.
* Full suite, Ruff and mypy remain the regression gate.

## Consequences

* The next application has a testable rule: put domain code in `applications/` or a
  host integration, compose through `sdk`, never edit core for a domain feature.
* The two shims are explicit debt with a removal target, not an invisible exception.
* F1c and F2 can proceed without reopening the boundary question.

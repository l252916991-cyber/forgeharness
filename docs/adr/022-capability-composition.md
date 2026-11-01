# ADR 022: capability composition for application specs

Date: 2026-10-18. Status: accepted for implementation.

## Need

`ApplicationSpec` currently holds the application identity, tools, policy, verifier,
budget, instructions and context providers in one frozen dataclass. That was adequate
for one reference application, but it makes every new application add fields or rebuild
the whole spec. The framework goal requires applications to be composed from reusable,
self-contained capabilities without turning the core into application-specific code.

The upstream study of Pydantic AI / Pydantic AI Harness (commit
`0be309f8a0d92f5868e1779c39f93f4e5f30f1ff`) showed the useful shape: tools, instructions,
context and hooks are capabilities, and a complete application (Coder/Researcher) is
itself a combination of capabilities that can be taken apart. That is design inspiration,
not copied code.

## Decision

Add a data-only `Capability` in `forgeharness.sdk.capability`:

```python
@dataclass(frozen=True)
class Capability:
    name: str
    tools: tuple[Tool, ...] = ()
    instructions: tuple[str, ...] = ()
    context_providers: tuple[ContextProvider, ...] = ()
```

Add `compose_application(...)`, which validates capabilities and builds the existing
`ApplicationSpec`:

* at least one capability is required;
* capability names and tool names must be unique across the application;
* a capability with no tools, instructions or context providers is rejected;
* application-level instructions/context providers come first, then capabilities in
  declaration order;
* `policy`, `verifier`, `budget`, `authorize_run` and `evidence_transform` remain explicit
  application-level inputs.

The existing `ApplicationSpec` constructor remains public and unchanged. Capabilities are
trusted Python objects registered by the host; there is no import path, plugin discovery,
or model-authored code.

## Why policy and verifier are not capability contributions yet

Combining multiple `ContextualPolicy` instances needs explicit semantics: does a policy
that does not understand a tool return ALLOW or abstain? Does REQUIRE_APPROVAL from one
policy survive an ALLOW from another? How does `can_approve` compose when each policy
only owns part of the resource space? A naive `all()` or `any()` would either grant too
much or break unrelated approvals.

The same applies to verifiers: `AllOfVerifier` is safe only when every verifier speaks
about the same final-answer contract. Domain verifiers often assume different payloads.
Those contributions need scoped applicability and a separate ADR; until then they stay
application-level. `Capability` is deliberately data, not a base class with hooks, so
this decision can be extended without breaking the composition API.

## Boundaries

* Core scope, policy, approval, budget, trace and verification remain mandatory; a
  capability cannot disable them.
* No dynamic imports, arbitrary plugin paths, or runtime-authored Python.
* No change to `Harness` orchestration in this ADR; it still runs the ReAct runtime.
  An `Orchestrator`/`RunKernel` port is a separate decision.
* No LexVault or commerce migration; applications adopt composition incrementally.
* Composition is by value/order, not by dependency graph; capability ordering is explicit
  and stable.

## Verification

Added `tests/unit/test_capability_composition.py` covering declaration order, application
instructions before capability instructions, duplicate tool names within and across
capabilities, duplicate capability names, empty capability, missing capabilities, and a
`Harness` end-to-end run with a composed spec.

Run:

```bash
uv run --locked pytest tests/unit/test_capability_composition.py tests/unit/test_sdk.py -q
uv run --locked ruff check src/forgeharness/sdk/capability.py tests/unit/test_capability_composition.py
uv run --locked mypy src/forgeharness/sdk/capability.py
```

## Consequences

* Applications can reuse tools/instructions/context without editing core; the
  `ApplicationSpec` surface does not grow for every feature.
* Existing applications are unaffected and do not need migration.
* Policy/verifier composition remains an explicit application concern until a scoped
  composition ADR exists.
* The next framework slice is F1b: `Orchestrator`/`RunKernel` injection, so a custom DAG
  can use the same governed model/tool/trace/budget/verifier path as ReAct.

# ADR 028: application composition audit record

Date: 2026-10-20. Status: accepted for implementation. Extends ADR 022.

## Need

`compose_application` merged capabilities into an `ApplicationSpec` but discarded
which capabilities produced it. A run's trace showed the application id/version and
the tools the model saw, but nothing tied a run back to its composition: two
different capability sets under the same manually-managed version were
indistinguishable after the fact. The framework invariant is that every
model-visible input must be reconstructable from the trace; the composition is part
of that input (instructions and tool set derive from it).

## Decision

* Add `ApplicationComposition` (frozen): ordered `capability_names` plus a `digest`.
* `ApplicationSpec` gains an optional trailing field
  `composition: ApplicationComposition | None = None`. Direct construction is
  unchanged and yields `None`; `dataclasses.replace` and existing call sites are
  unaffected.
* `compose_application` computes
  `digest = sha256(canonical JSON(capability names, tool names, instruction texts,
  context-provider type names))` — all in merged order.
* `Harness._run` emits one `application.composition` trace event (names + digest
  only) before context compilation, so both the ReAct path and custom-orchestrator
  runs carry it. The event contains no instruction text or case content.

## Boundaries

* The digest is a **structural** identity: it detects renamed/reordered
  capabilities, changed tool names, changed instruction text and changed provider
  types. It does **not** hash tool/provider implementations; a behavior change
  inside unchanged code still requires the host to bump `ApplicationSpec.version`.
* Composition does not become an authorization input; scope, policy and approval
  are unchanged.
* Apps built by direct `ApplicationSpec(...)` construction legitimately have no
  composition record; the trace simply omits the event.

## Migration

All reference apps now compose through capabilities, so every reference app records
a composition:

* `applications/legal/pilot.py` (ADR 024 round),
* `applications/legal/lexvault` (ADR 025 round),
* `applications/commerce/application.py` (both factories),
* `applications/commerce/consultation.py`,
* `applications/records.py` (read and cited-draft specs),
* `coding/application.py`.

## Verification

* `test_capability_composition.py`: composition recorded with ordered names; digest
  deterministic for equivalent compositions; sensitive to capability order and
  instruction text; direct spec has `composition is None`; a composed harness run
  writes `application.composition` with the digest into the trace.
* Existing records/consultation/coding/legal/commerce suites pass unmodified,
  proving the migration preserved behavior.

# ADR 043: Prompt caching with an exact admitted wire snapshot

Date: 2026-10-30. Status: implemented.

## Context

Every request repeats the same prefix: the system instructions, the pinned todo board,
the tool catalogue, the verifier contract. That prefix was paid for at full price on
every single step. A 50-step run with an 8-15k token prefix buys 400k-750k input tokens
of material the provider had already seen.

The reason this mattered beyond cost is that it distorted comparison. An evaluation that
measures ForgeHarness against another harness on the same tasks would attribute the
difference to design, when part of it was a missing header.

## Decision

`models/caching.py` computes a `CachePlan`; the adapter renders markers during preparation:

- `CacheMode` names three provider behaviours rather than one feature: `OFF`, `PREFIX`
  (the provider caches prefixes automatically, so the only job is to keep the prefix
  stable) and `EXPLICIT` (the provider needs a marker on the last element to cache).
- The stable prefix is the leading run of SYSTEM messages. Tools are a separate
  directive target because every provider treats the tool catalogue as its own cacheable
  block.
- `apply_cache_plan()` returns a copy with `cache_control` attached. The adapter
  freezes this complete body using `chat-completions-cache-explicit-v1`, whose
  validation allows exactly the planned tool/system markers while binding every
  semantic message and tool to the runtime request. `RequestBudgetEstimate`,
  evidence and PG canonical input all include the markers. Dispatch sends the
  frozen `payload_json` byte-for-byte, including if adapter configuration changes
  after admission. There is no transport-only rewrite.
- `cache_report()` emits integers only (`cached_fraction_milli`), so a trace assertion
  can compare a plan exactly instead of within a float tolerance.

The default for `OpenAICompatibleConfig.cache_mode` is `OFF`. An unknown gateway may
reject an unexpected field, and a harness that breaks on a provider it has never seen is
worse than one that pays full price. `EXPLICIT` is an operator decision.

Two properties make the prefix actually stable, and both were already true and are now
tested: `ToolRegistry.specs()` sorts by name, so registration order does not leak into
the request; and the pinned todo board sits at a fixed position directly after leading
instructions rather than being appended to history.

Observation, not inference: `dispatch_model_once` records `model.cache` with the plan and,
when the provider reports a hit, `model.cache.observed` with `cached_input_tokens`. Both
are read through `getattr`, so a model without the capability simply produces no event.
The SDK `ModelBridge` forwards validated cache plans and provider hit accounting;
providers without the capability receive an OFF plan and report no observed hit.

## Consequences

Cache hits are visible in the trace, which is what makes the cost claim auditable rather
than asserted. `Usage` and `ModelUsage` keep their existing fields: cached tokens are
provider reporting, not harness accounting, and adding a field would have broken the
exact-usage comparisons that the execution contract tests rely on.

`apply_cache_plan` owns the marker shape; `render_payload` remains a pure candidate
rendering utility, not a dispatch hook. Another marker format needs an explicit
prepared validation contract. Real loopback HTTP tests compare actual request bytes
and their digest with the admitted payload in OFF/PREFIX/EXPLICIT modes.

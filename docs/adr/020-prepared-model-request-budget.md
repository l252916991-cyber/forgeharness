# ADR 020: Preflight immutable complete model inputs

Date: 2026-10-17. Status: accepted for local runtime/SDK invocations.

## Context and attribution

ADR 019 fixed material rendering only. Runtime conversation assembly still omitted
tool definitions, JSON framing, provider options and declared output reservation.
The upstream design study identified complete request accounting as the next slice.
Design influence: [Deep Agents full-input counting](https://github.com/langchain-ai/deepagents/blob/c5313d7005534c8f8514004890c6f6d5174c6e2f/libs/deepagents/deepagents/middleware/summarization.py#L1034)
and [Pydantic AI schema-disclosure budget regression](https://github.com/pydantic/pydantic-ai/blob/2c23dd01b804dd238e52f70754cdbdb7b8ace70d/tests/harness/compaction/test_context_budget.py#L817).
No upstream code or dependencies were imported.

## Decision

Keep `Model.decide` compatible. Add an optional PreparingModel protocol with a
validated PreparedModelCall containing detached immutable JSON, declared limits
and a format identifier. Plain Models use an explicit generic request snapshot.
The chat-completions adapter sends admitted UTF-8 bytes directly and shares input
serialization with validation. Runtime messages/tools and declared output caps
cannot silently differ from that supported prepared format.

Preflight the complete payload using the deterministic character estimator; add
host and declared opaque overhead, then the larger host/adapter output reservation.
Use the smaller host/declared provider window. Reserve non-message costs when
reassembling intact history, bounded to three local preparation attempts. Never
call a model on overflow or silently enlarge a budget. Recompute every step and
after resume/protocol correction; apply to both planner and executor.

Trace the admitted payload and SHA-256 with component estimates and unknown flags.
Rejected preflight candidates are not model.request events. SDK privacy checks
decode JSON before checking credential-bearing keys, rather than only inspecting
serialized strings. HTTP tests prove the admitted body equals the actual mock
transport body. Preparation has no authorized side effects or network calls.

## Consequences

- JSON coverage is complete for the declared input; token counts are heuristic,
  not actual tokenizer limits, billing or a guarantee across Chinese/templates.
- Generic models still hide provider-specific transformations; those limits are
  explicitly unknown. Custom formats require trusted adapter-specific mapping.
- Output reservation does not set a provider output cap. Cumulative actual usage
  checks and side-effect recovery remain separate contracts.
- Additional serialization/deserialization and up to three local compaction
  attempts cost CPU. Capacity selection is conservative, not optimal packing.
- Context limits that formerly admitted a message-only transcript may now fail.
  Existing compaction tests use a larger full-input window and additionally check
  that sent requests actually fit; the stricter tool-schema rejection is retained.
- Legacy standalone RAG/chat and external provider-internal additions are outside
  the declared scope. No runtime model/endpoint auto-detection or new dependency.

Details and configuration: [REQUEST_PREFLIGHT](../REQUEST_PREFLIGHT.md).

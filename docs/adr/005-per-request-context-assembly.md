# ADR 005: Per-request context assembly and deterministic history compaction

Status: accepted.

## Context

The runtime keeps the full conversation in its checkpoint so a run is resumable and auditable. Before this decision, every model request sent that conversation verbatim and used `RunBudget.max_input_tokens` only as a cumulative kill switch: once usage crossed the cap the run moved to `EXHAUSTED`. A long run therefore died rather than adapting, and the token budget never influenced what the model saw on a given step.

`ContextCompiler` already selected static fragments (for example the AST repository map) under a budget, but it ran once at `CodingAgent.start` and had no view of the growing conversation.

## Decision

Add a `ContextAssembler` (`src/forgeharness/context/assembly.py`) and call it once per model request inside `AgentRuntime._drive`.

- A new `RunBudget.max_context_tokens` caps a single assembled request, separate from the cumulative `max_input_tokens` cap.
- Assembly is a pure function of the message list: leading system instructions and the newest turns are always kept verbatim; older turns are folded into one summary message. The same input yields byte-identical output.
- Grouping is turn-structural: an assistant tool-call message and the tool results that follow it are one indivisible unit, so compaction never orphans a tool result.
- The summary is deterministic and states how many messages were elided. It is emitted as a `system` message but labelled a derived record of untrusted history, never instructions.
- If the required context (leading system messages plus the minimum recent turns) cannot fit, assembly raises `ContextBudgetError`; the runtime records `context.overflow` and fails the run with an explicit error instead of silently truncating.
- Each compaction appends a `context.compacted` trace event carrying the excluded decisions, so the elision is reconstructable from the trace while the checkpoint retains the full transcript.

## Consequences

The model now sees a bounded, reproducible window and long runs degrade gracefully instead of exhausting. The full conversation remains the checkpoint's source of truth and the summary is always labelled as derived.

Compaction is deterministic and structural, not semantic: it does not decide what mattered. Semantic or task-aware compaction, progressive retrieval, and tool-subset retrieval remain open and must each clear an evaluation gate before they are added, per the product invariant that complexity must earn itself.

The assembler is injected like `ObservationCompressor`, so the runtime default follows `RunBudget`, and tests may supply a different estimator or window.

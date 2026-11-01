# ADR 019: Budget the rendered material projection

Date: 2026-10-16. Status: accepted for the local embedded SDK.

## Problem and upstream influence

`ContextCompiler` charged only a material's content, then added source wrappers and
separators after selection. A one-character body with a long source name was
reported as one estimated token although the outgoing block cost 40 under the
same estimator. Required material could therefore bypass the declared material
budget, even before considering the rest of the model request.

This defect was identified while comparing full-input accounting in
[Deep Agents `_count_tokens`](https://github.com/langchain-ai/deepagents/blob/c5313d7005534c8f8514004890c6f6d5174c6e2f/libs/deepagents/deepagents/middleware/summarization.py#L1034)
and [Pydantic AI's context-budget tests](https://github.com/pydantic/pydantic-ai/blob/2c23dd01b804dd238e52f70754cdbdb7b8ace70d/tests/harness/compaction/test_context_budget.py#L817).
These implementations informed the review question; no upstream implementation
was copied or introduced as a runtime dependency. Both projects use MIT licenses;
the research bundle retains their licenses and exact source blob identities.

## Decision

Preserve required-first/relevance ordering and whole-fragment selection. For each
candidate, render exactly the source wrapper and separator that would be sent,
then estimate the entire candidate text with the configured estimator. Admit it
only when that total fits. Reject required overflow before model invocation;
skip optional overflow and continue considering smaller candidates.

`CompiledContext.estimated_tokens` describes its exact `text` under that estimator.
A decision's `estimated_tokens` describes its isolated rendered addition,
including a separator when appended after previously selected material. These
per-item estimates need not sum to the total: token boundaries and rounding may
change when fragments are joined. No new fields or database migration are needed.

## Consequences and limits

- The default character estimator remains a heuristic, including for Chinese.
  It is not a conservative bound on a real model tokenizer.
- This fixes the material projection only. Conversation assembly does not yet
  reserve all tool schemas, provider framing or output tokens. Full model-request
  accounting remains a separate enhancement, not a claim of this change.
- Re-estimating each complete candidate avoids assuming an additive tokenizer.
  Work can grow quadratically with selected text size; the current bounded local
  material use is accepted. A future optimization must preserve exact projection
  accounting and be supported by a measured workload.
- Existing budgets may admit fewer optional fragments or reject required
  fragments previously accepted through undercounting. Raising a budget is a
  host decision; the compiler does not silently weaken it.

Regression tests cover required wrapper overflow, optional overflow followed by a
smaller fitting item, separator boundaries, joined-estimate rounding, empty
input, unchanged priority ordering and the public SDK's no-model-call boundary.

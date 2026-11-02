# 048 — State the one-call-per-step rule in every tool request

Date: 2026-11-02. Status: accepted.

## Context

The runtime executes exactly one tool call per model response; a response with
several calls is rejected as a recoverable protocol violation, nothing in it runs,
and the rejected attempt still costs a step. The rule was communicated only through
the request parameter `parallel_tool_calls: false`.

DeepSeek's API does not honour that parameter. In the `deepseek-flash` diagnosis
run, five of the twelve captured ReAct failures contained such a violation (one
response bundled four reads). In `long-five-step` the rejected response was the only
step over the case's budget.

## Decision

`AgentRuntime` pins a short, harness-owned system block into every request that
offers tools, through the same mechanism as the todo board:

> Harness protocol: send at most one tool call per response, then wait for its
> result before choosing the next step. A response containing several tool calls
> is rejected and none of them runs.

The block states only a rule the runtime enforces and names no tool or task, so it
cannot steer a decision. It is part of each traced request and never part of the
transcript, so checkpoints, resumed histories and compaction are unchanged. Hosts
can pass `protocol_notice=False`.

## Consequences

Every tool request carries about 45 extra input tokens. Providers that do honour
`parallel_tool_calls` see a redundant statement of the same rule. Live benchmark
reports made before and after this change differ by revision, not only by model.

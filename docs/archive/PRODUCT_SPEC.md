# Product specification

This document defines the product direction. The exact audited implemented/planned boundary is maintained in [`STATUS.md`](STATUS.md).

## Problem

Most portfolio agents demonstrate a prompt and a tool call but do not prove that the runtime controls state, permissions, context, failure recovery, or evaluation. ForgeHarness must demonstrate those Harness responsibilities through one objective vertical task: resolving a repository issue with a tested patch.

## Users

- A developer who wants a local coding agent with inspectable behavior.
- An Agent engineer who wants to compare loop, context, and delegation strategies.
- An interviewer who needs reproducible evidence for the candidate's claims.

## Primary workflow

1. The user selects a Git repository and provides an issue statement.
2. The user selects a disposable branch/worktree; ForgeHarness records the task and confines file tools to that repository.
3. The runtime compiles budgeted context and asks the selected loop for its next action.
4. Tools are schema-validated, policy-checked, executed with deadlines, and traced.
5. The agent explores, edits, and tests until it submits a patch or reaches a budget limit.
6. The agent runs the configured tests, inspects the diff, and reports verification evidence.
7. ForgeHarness persists the checkpoint and trace; patch-file export and a dedicated reviewer stage remain planned.

## Functional requirements

### Harness runtime

- Model-neutral request/response interface with deterministic test doubles.
- ReAct and Plan-Execute loops sharing the same runtime contracts.
- Step, token, and tool-call budgets plus per-tool execution timeouts.
- Explicit terminal states: succeeded, failed, exhausted, cancelled, and awaiting approval.
- Persistent checkpoints that can resume without replaying side effects.

### Tools and safety

- Typed registry with JSON-schema-compatible inputs and structured results.
- Workspace-scoped filesystem and search tools.
- Command execution with timeout, output limit, environment filtering, and policy decisions.
- Approval records bound to a task, tool name, normalized arguments, and expiration.

### Context engineering

- Stable instructions separated from dynamic task context.
- Per-section token budgets and deterministic overflow behavior.
- Repository map, lexical search, symbol extraction, and relevance scoring.
- Observation compression that preserves errors, file locations, and test summaries.
- Trace metadata showing why each context item was included or discarded.

### State and memory

- Append-only execution trace and checkpoint snapshots.
- Short-term working state scoped to one run.
- Long-term lessons stored as source-linked, reviewable records rather than invisible prompt text.

### Coding vertical

- Repository exploration, editing, test execution, diff inspection, and evidence reporting.
- User-selected Git repository boundary with a recommendation to use a disposable worktree.
- A future reviewer stage must not silently approve without test or risk evidence.

### Extensibility

- Local Skills with versioned metadata and explicit capabilities.
- MCP client support through the same policy and tracing path as native tools.
- Sub-agent delegation with a reduced tool scope and a parent-owned budget.

## Non-functional requirements

- Python 3.12, strict typing, and keyless deterministic tests.
- No core dependency on LangGraph, AutoGen, or another Agent orchestration framework.
- Secret values never appear in traces or exported reports.
- Core unit test coverage at or above 85%, plus integration and evaluation cases.
- A clean setup path on macOS/Linux with `uv`; Docker-dependent tests may be marked separately.

## Explicitly out of scope for the first audited release

- Autonomous merge or deployment to production.
- Arbitrary host command execution without an isolated workspace and policy.
- Training or fine-tuning a foundation model.
- Claiming that multi-agent is better unless the included evaluation demonstrates it.
- A2A support before the native, MCP, and sub-agent execution paths are stable.

## Release acceptance

The first audited release is acceptable when a clean environment can run the keyless demo and control evaluation, reproduce the approval-gated fixture repair, block scoped unsafe operations, persist checkpoints, verify a trace, expose the configured real-model CLI path, and pass the review protocol. Real-model quality claims require a separately disclosed credentialed report.

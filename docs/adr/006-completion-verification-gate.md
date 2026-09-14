# ADR 006: Harness-owned verification gate before a run may succeed

Status: accepted.

## Context

`AgentRuntime._drive` ended a run the moment the model emitted a `FinalAction`: the model's assertion that it had finished was accepted as fact and the run moved to `SUCCEEDED`. Every other control in the loop — policy, budget, approval, checkpoint, trace — was Harness-owned, but the single most consequential transition, "the task is done", was prompt-driven. The only completion check in the repository lived in the LangChain comparison agent (`_check_completion`) and was a heuristic that scanned recent message text for `"Exit code: 0"`; the framework-independent runtime had none.

The product invariant requires deterministic runtime state to live outside model conversation text. Completion is runtime state.

## Decision

Add a `Verifier` protocol (`src/forgeharness/runtime/verification.py`) and call it inside `AgentRuntime._drive` at the `FinalAction` branch, before any successful termination.

- The verifier receives a `VerificationRequest` containing the final action, the workspace path, and a tuple of `ToolEvidence` — the structured `ok`, `content`, and `metadata` of every executed tool call, captured by the runtime as it dispatches. It does **not** receive the chat transcript and must not parse message strings.
- It returns a `VerificationResult` (`passed`, `reason`, `evidence`). It owns no loop control: the runtime alone decides what happens next.
- On `passed`, the run finishes `SUCCEEDED`. On rejection, the runtime appends the reason and evidence back to the conversation as a `user` message, saves a running checkpoint, and continues `_drive`. A run that is never verified exhausts its normal step/token budget.
- A verifier that raises fails the run (`verification.failed`) rather than silently passing it.
- The gate is opt-in at the runtime (`verifier=None` preserves the previous behavior). The coding vertical (`CodingAgent`, CLI repair, API handler) installs a `CodingVerifier` by default; `NoopVerifier` is available where a path wants the gate wired but no check.
- `CodingVerifier` requires a successful `write_file` and a passing `run_tests` in the tool evidence. It checks structured results, never strings — the failure of that LangChain heuristic is the reason this exists.

## Consequences

"Model claims done" becomes "Harness confirms done against evidence the Harness already holds". Because the verifier only reads tool results and the workspace, its verdict is reconstructable from the trace.

Verification is only as strong as the evidence a task type can produce. The coding vertical has tests and write results; other verticals (knowledge, general chat) are not gated yet and keep the prior behavior. Task-type verifiers such as citation or file-existence checks are deliberately not added until a vertical needs them, per the invariant that multi-component complexity must earn itself through evaluation.

# ADR 023: application-provided orchestrators and a governed run kernel

Date: 2026-10-18. Status: accepted for implementation.

## Need

`Harness` previously built `AgentRuntime` unconditionally, so every run was the
ReAct loop. LexVault's deterministic parallel specialist DAG could not use the
framework's scope, budget, trace, verification or checkpoint path without either
rewriting the DAG as ReAct (losing parallelism and determinism) or running outside
`Harness` (bypassing governance).

The upstream study of ADK `Workflow` (graph nodes, parallel branches, join,
interrupt/resume), LangGraph `StateGraph`/checkpoint and Pydantic AI's typed agent
loop informed the separation of "control flow" from "governed calls". No upstream
code was copied.

## Decision

Add an application-owned `Orchestrator` Protocol and a `RunKernel`:

```python
class Orchestrator(Protocol):
    async def run(self, *, kernel: RunKernel, task: str) -> RunResult: ...
```

`Harness(application, state_dir=..., orchestrator=...)` builds a kernel instead of
`AgentRuntime` when an orchestrator is supplied. With no orchestrator the existing
ReAct path is unchanged.

`RunKernel` exposes:

* `call_model(messages=..., phase=...)` through `prepare_request` (context budget,
  compaction, prepared-input validation), the evidence transform and hash-chained
  trace; usage is accounted per call.
* `call_tool(call)` only for `RiskLevel.READ` tools, through the same
  `BoundPolicy` and `ToolDispatcher` as ReAct. A write/process/network tool, or a
  policy decision other than ALLOW, raises `KernelToolDenied` before execution.
* `verify(final)`, `checkpoint()`, `emit(...)`, `finish(...)`; success requires a
  passing `Verifier`.
* `remaining` budget and the current transcript/evidence; the orchestrator owns
  message construction and control flow.

## Boundaries

* Custom orchestrators are one-shot. `Harness.resume` and `Harness.approve` raise
  `ScopeError` for them; approval delegation and invocation-journal recovery remain
  ReAct-only.
* Only read-only tools are callable. There is no journal, retry, approval or
  side-effect recovery in the custom path; pretending otherwise would create a
  weaker second contract.
* A custom run has `recovery_semantics_version=0`; it is not resumable by the ReAct
  recovery path.
* The kernel cannot disable scope validation, the evidence transform, budget,
  trace, checkpoints or the verifier.
* No dynamic import, plugin loading or model-authored orchestrator code.
* `ApplicationSpec` is unchanged.

## Verification

`tests/unit/test_orchestration_kernel.py` covers a two-node parallel DAG, a
read-only tool with policy evidence, a rejected write tool, step-budget exhaustion,
verifier rejection, unfinished orchestrator, orchestrator exception and refused
resume. The existing SDK/preflight/capability suites remain the regression gate for
the ReAct path.

## Consequences

* LexVault can implement `LexVaultDagOrchestrator` over `RunKernel.call_model` and
  host services without changing the core or pretending its DAG is ReAct.
* Write/approval orchestration is the next slice (F1c): extract the existing
  approval/journal execution semantics rather than duplicating them.
* Custom runs are intentionally less capable than ReAct today; the limitation is
  explicit and testable, not silent.

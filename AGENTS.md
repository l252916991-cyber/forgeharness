# ForgeHarness Repository Guide

## Product invariants

- ForgeHarness is an Agent Harness first and a coding-agent application second.
- Deterministic runtime state, budgets, approvals, and permissions must not live only in model conversation text.
- Tool arguments and model outputs are untrusted at their boundaries and require validation.
- A task may modify only its explicitly scoped workspace.
- Every model-visible input and tool-side effect must be reconstructable from the trace.
- Multi-agent behavior must earn its complexity through evaluation, not through feature count.

## Engineering rules

- Target Python 3.12 and manage the environment with `uv`.
- Keep the implementation framework-independent; do not use LangGraph for the core runtime.
- Add tests with every behavior change and defect fix.
- Record non-trivial architecture decisions under `docs/adr/`.
- Never commit model credentials, GitHub tokens, private repository contents, or raw secrets in traces.
- Preserve upstream attribution when an external implementation influences code or design.

## Completion rule

The project is not complete when the demo merely works. Before completion:

1. Freeze an audit candidate and run all quality and evaluation gates.
2. Review architecture, correctness, tests, security boundaries, dependencies, documentation, and resume claims.
3. Classify every finding; fix blocker/high findings and add regression tests for confirmed defects.
4. Rerun the full verification suite from a clean checkout-equivalent state.
5. Document accepted residual risks and the precise implemented/planned boundary.


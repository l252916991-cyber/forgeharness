# Final review protocol

The final review is a release gate, not a documentation exercise.

## Review dimensions

### Architecture

- Core Harness decisions are not coupled to CLI/API/provider adapters.
- State, permissions, budgets, and approval records are deterministic and durable.
- Native, MCP, and sub-agent paths reuse policy and observability controls.

### Correctness and resilience

- Terminal states are exhaustive and transitions are valid.
- Timeout, cancellation, retry, crash, and resume behavior are tested.
- Side effects cannot be duplicated by checkpoint recovery.

### Security boundaries

- Workspace traversal, command injection, environment leakage, prompt injection, and trace-secret exposure are tested.
- Approval is scoped to exact normalized actions and expires.
- External content is data, never executable Harness instruction.

### Tests and evaluation

- Unit, integration, control evaluation, and fixture-repair tests cover release claims.
- Coverage exclusions are justified.
- Benchmark reports are reproducible and include failures.

### Dependencies and operations

- Dependencies are maintained, pinned within compatible ranges, and license-compatible.
- Setup, Docker, migrations, failure recovery, and cleanup are documented.
- CI runs keyless gates; credentialed evaluations are optional and clearly separated.

### Documentation and resume claims

- README and architecture describe current behavior.
- Every quantitative claim links to a revision-bound report.
- Upstream influence and reused data are attributed.
- Planned features are not written as completed work.

## Severity

- Blocker: invalidates safety, evaluation, or core functionality; release stops.
- High: likely incorrect behavior or unsupported public claim; must fix.
- Medium: meaningful maintainability or edge-case issue; fix or document with owner.
- Low: polish or low-risk improvement; may enter the backlog.

## Evidence produced

The audit candidate must produce a machine-readable findings file, a human review report, test/evaluation reports, the reviewed Git revision, an exact source snapshot manifest, and a residual-risk statement. `forge review` recomputes every candidate file hash and checks the available report revisions, workload/quality thresholds, pure branch coverage, dependency findings, and unresolved critical issues. It does not cryptographically prove when older reports ran; a final freeze requires a fresh run or explicit tested-source binding, not merely matching `git HEAD` in a dirty working tree.

The current audit decision is [`review/AUDIT.md`](review/AUDIT.md) with machine findings in [`review/FINDINGS.json`](review/FINDINGS.json). The complete evidence set is under [`../reports/`](../reports/), including `candidate-manifest.json`; generated reports and the decision directory are intentionally excluded from the source-tree hash to avoid circular evidence.

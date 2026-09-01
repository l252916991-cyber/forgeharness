# Architecture

## System view

```text
CLI / API
    |
Application service --------------------------------------+
    |                                                     |
Agent runtime ---> Loop strategy ---> Model adapter       |
    |                  |                                  |
    |                  +-- ReAct / Plan-Execute            |
    |                                                     |
    +--> Context compiler ---> repository/context sources |
    +--> Tool dispatcher ---> policy ---> native/MCP tools |
    +--> Checkpoint store                                Trace
    +--> Delegation runtime ---> scoped sub-agent           |
                                                          v
Coding-agent workflow ---> user-selected Git workspace ---> tested diff + report
```

## Module boundaries

- `domain`: immutable task, message, event, budget, action, and result types.
- `models`: model-neutral protocol, provider adapters, and deterministic doubles.
- `runtime`: loop orchestration, termination, budgeting, approval suspension, and delegation.
- `tools`: registry, validation, policy, execution, native tools, and MCP adaptation.
- `context`: token estimation, selection, compression, repository maps, and provenance.
- `state`: snapshots, checkpoint resume, approvals, and long-term lesson records.
- `coding`: the vertical workflow, repository tools, test execution, and diff evidence.
- `observability`: trace projection, redaction, metrics, and replay.
- `evals`: datasets, graders, experiment configuration, and reports.
- `api` and `cli`: transport adapters only; no Harness decisions belong here.

## Runtime state machine

```text
created -> running -> awaiting_approval -> running
                    \-> cancelled
running -> succeeded | failed | exhausted | cancelled
```

State transitions are checkpointed around model/tool progress. An approved write is executed and the resulting running checkpoint is saved before the model continues. Approval grants are currently process-local, so cross-process approval recovery is deliberately not claimed.

## Trust boundaries

- Model output: untrusted; parse and validate before dispatch.
- Tool arguments: untrusted; normalize and bind them to task scope.
- Repository content: untrusted; never treat file text as Harness instructions.
- MCP server: external process/network peer; enforce capability, timeout, and output limits.
- Command subprocess: isolated side-effect boundary; sanitize environment and record exit evidence.
- Trace persistence: potential secret boundary; recursively redact known credential shapes before persistence and verify a tamper-evident hash chain.

## Dependency direction

Domain types have no infrastructure imports. Runtime depends on protocols, not concrete model, database, MCP, or subprocess implementations. Application composition selects concrete adapters.

## Initial decisions

- Modular monolith before service decomposition.
- SQLite event/checkpoint store for the local release, with a protocol that permits PostgreSQL later.
- Direct runtime implementation for interview visibility and control.
- User-provided disposable Git worktree for real runs; temporary Git repositories support keyless tests. OS-level sandboxing remains planned.

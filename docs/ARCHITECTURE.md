# Architecture

## System view

```text
CLI / API / worker
        |
Application composition (sdk): ApplicationSpec + Capability + Orchestrator
        |
Harness / RunKernel: scope, policy, approval, budget, checkpoint, trace, verification
        |
        +--> AgentRuntime (ReAct) ---------> model / tools / context
        +--> Application orchestrator -----> model / read-only tools / host services
        |
Host integrations: database, auth, retrieval, files, queues
        |
Reference applications: legal, commerce, coding
```

The framework core is application-neutral. A reference application composes
`Capability` objects and either uses the default ReAct runtime or supplies an
`Orchestrator` over `RunKernel` (ADR 023). Coding is one reference application, not
the framework's default shape.

## Module boundaries

- `domain`: immutable task, message, event, budget, action, and result types.
- `models`: model-neutral protocol, provider adapters, and deterministic doubles.
- `runtime`: the default ReAct executor plus termination, budgeting, approval suspension, delegation, and completion verification.
- `tools`: registry, validation, policy, execution, native tools, and MCP adaptation.
- `context`: token estimation, per-request window assembly, selection, compression, repository maps, and provenance.
- `state`: snapshots, checkpoint resume, approvals, and long-term lesson records.
- `sdk`: public application contracts and composition: `ApplicationSpec`, `Capability`, `Harness`, `Orchestrator`, `RunKernel`, records, sessions, imports, tasks and workflows.
- `applications`: reference applications (`legal`, `commerce`) built on the public SDK; they are not core.
- `coding`: one optional reference vertical (workspace, repository tools, tests, diff evidence); not the framework default.
- `observability`: trace projection, redaction, metrics, and replay.
- `evals`: datasets, graders, experiment configuration, and reports.
- `api` and `cli`: transport adapters only; no Harness decisions belong here.
- `integrations` (host-owned, not a core package): database, auth, retrieval, files and queue adapters.

## Runtime state machine

```text
created -> running -> awaiting_approval -> running
                    \-> cancelled
running -> succeeded | failed | exhausted | cancelled
```

State transitions are checkpointed around model/tool progress. An approved write is executed and the resulting running checkpoint is saved before the model continues. Approval grants are currently process-local, so cross-process approval recovery is deliberately not claimed.

`running -> succeeded` is gated by an injected `Verifier` (ADR 006). The runtime captures each dispatched tool's structured result as evidence and, on a model `FinalAction`, asks the verifier to confirm the claim. A rejection is returned to the model as a message and the loop continues; a verifier that raises fails the run. The gate is installed by application composition; the coding reference app's verifier requires a successful write and passing tests, while the legal app's requires source-bound citations.

## Trust boundaries

- Model output: untrusted; parse and validate before dispatch.
- Tool arguments: untrusted; normalize and bind them to task scope.
- Repository/case/document content: untrusted; never treat host-provided text as Harness instructions.
- MCP server: external process/network peer; enforce capability, timeout, and output limits.
- Command subprocess: isolated side-effect boundary; sanitize environment and record exit evidence.
- Trace persistence: potential secret boundary; recursively redact known credential shapes before persistence and verify a tamper-evident hash chain.

## Dependency direction

```text
applications / host integrations
        |
        v
       sdk  (public contracts and composition)
        |
        v
runtime / domain / tools / context / state / observability
```

Core code must not import `forgeharness.applications` or `forgeharness.coding`.
Three API-v1 compatibility shims are explicitly frozen and tested:
`runtime/verification.py` (lazy `CodingVerifier`), `context/__init__.py` (lazy
`PythonRepositoryMap`) and `context/repository.py` (deprecated re-export module;
the third shim was surfaced by the dynamic-import scan, see ADR 027). Remove them
at API v2; do not add new core -> application imports.
`tests/unit/test_import_boundaries.py` enforces the rule, including
`import_module("...")` string literals.

Domain types have no infrastructure imports. Runtime depends on protocols, not
concrete model, database, MCP, or subprocess implementations. Application
composition selects concrete adapters.

## Initial decisions

- Modular monolith before service decomposition.
- SQLite event/checkpoint store for the local release, with a protocol that permits PostgreSQL later.
- Direct runtime implementation for interview visibility and control.
- User-provided disposable Git worktree for real runs; temporary Git repositories support keyless tests. OS-level sandboxing remains planned.

## R&D knowledge-agent extension

```text
POST message
   -> SessionStore
   -> ModelIntentRouter --invalid/timeout--> RuleIntentRouter
      -> knowledge_query: FTS5 || Embedding/Qdrant -> RRF -> Reranker
                           -> grounded draft -> optional read-only Reviewer
      -> coding_task: verified Git binding -> CodingAgent -> exact approval
      -> memory_command: candidate -> human review -> isolated memory indexes
      -> general_chat: OMLX chat
```

Uploads are content-addressed with SHA-256. The API atomically stores bytes before queue submission. An idempotency key maps to exactly one content digest; reusing it for other bytes is a conflict. A worker can safely receive an already-succeeded job again without re-indexing it.

`RetrievalReport` exposes lexical, vector, fused and final candidates plus stage timings. Citations are reconstructed from stored chunks rather than generated by the model. Knowledge and memory use separate collections whose names include an embedding-model fingerprint and dimension.

Knowledge answers persist a separate `AgentResponse` (not a fabricated CodingAgent checkpoint), and record request, retrieval stages, model input/output, and completion/failure in the existing hash chain. `GET /v1/runs/{id}` resolves both result types. Explicit attachments resolve immutable document IDs and use bounded source-order excerpts from those documents only; they do not append hashes to an unrelated global search query. Both direct memory search and knowledge-answer memory retrieval pass through the durable approved/indexed filter.

The knowledge/RAG and chat paths still need whole-conversation context compilation/summarization and ordinary-chat run tracing; the runtime's per-request window assembly (ADR 005) does not cover them. See the explicit full-plan gaps in `STATUS.md`.

The default learning profile is SQLite + FTS5 + in-memory vectors + inline queue. The platform profile selects asyncpg/PostgreSQL, Redis/ARQ and Qdrant. OMLX, two FastAPI processes and the Worker stay on the macOS host; Nginx and data services run in Compose. Liveness checks only the process, while readiness actively probes every configured dependency.

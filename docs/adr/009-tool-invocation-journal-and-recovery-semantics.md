# ADR 009: Tool Invocation Journal and Recovery Semantics

Status: accepted. Implemented in M10-A (see [`M10A_INVOCATION_JOURNAL.md`](../M10A_INVOCATION_JOURNAL.md));
the retry half of the recovery matrix is deferred to M10-B by design.

## Context

The runtime persists control-flow state in the checkpoint (`RunResult` with an optimistic CAS revision) and records observable evidence in the hash-chained trace. Neither is a record of **tool side effects**, which is what a resumed run actually needs.

Today the loop dispatches a tool and saves a running checkpoint afterwards. If the process dies between "the tool ran" and "the checkpoint was saved", recovery has no durable fact about that call: the checkpoint shows only the state *before* the dispatch. Nothing in the repository can answer "did this write happen?". The approval-resume path (`resume_approved`) already claims the checkpoint revision before any side effect, which rejects duplicate *approvals*, but that is control-flow protection, not a side-effect record.

ADR 006 established that completion is runtime state rather than model assertion. This ADR extends the same principle: whether a side effect occurred is runtime state, and it must be durable before the side effect begins.

The goal is deliberately **not** exactly-once delivery. General runtimes cannot provide it: between "started" and "the side effect happened" there is a window in which the runtime cannot know whether the external effect occurred. The goal is **fail-closed recovery**: never automatically re-run a side effect whose completion cannot be confirmed.

## Decision

### Authority: three sources, one per question

| Question | Authority |
| --- | --- |
| What was the control-flow state? | **Checkpoint** (`RunResult`) |
| Did this tool side effect happen, and what was its result? | **Invocation Journal** |
| What is the observable evidence of the run? | **Trace** |

The trace is evidence, **not** a recovery source of truth. It is redacted, it is append-only for audit, and a missing `tool.completed` event must never be read as "the tool did not run". Recovery reads the journal only.

### Tool effect class

Add `effect_class` to `ToolSpec` with three values:

| Class | Meaning | Recovery |
| --- | --- | --- |
| `read_only` | No external side effect. | Safe to re-execute. |
| `idempotent` | Has a side effect, and the tool genuinely honours a stable idempotency key: repeating with the same key produces no second business effect. | Safe to replay with the **same** key. |
| `non_idempotent` | Repeated execution cannot be shown to be safe. | Never automatically replayed. |

**Default is `non_idempotent` (fail-closed).** A tool must opt in to a weaker guarantee.

Two constraints make this conservative on purpose:

- A runtime-generated UUID does not make a tool idempotent. The tool implementation must actually consume the key and deduplicate on it. If that cannot be demonstrated, the tool is `non_idempotent`.
- `write_file` is **not** declared `idempotent` in this change. Its optimistic `expected_sha256` check makes a repeat fail rather than duplicate, which is not the same as idempotency under a key; until that is proven it stays `non_idempotent`.

Initial assignments: `echo`, `list_files`, `read_file`, `search_code`, `git_diff` are `read_only`; `write_file`, `run_tests`, and MCP tools are `non_idempotent`. Unknown tools are `non_idempotent`.

### Invocation journal

A durable record per **logical** tool call, in the same SQLite database as the checkpoint.

```
tool_invocations
  invocation_id       primary key
  run_id
  logical_call_id     stable across resume; identifies the agent's call
  tool_name
  effect_class
  args_digest         sha256 of canonicalised arguments
  idempotency_key     stable; never regenerated on resume
  state               claimed | started | completed | failed | indeterminate
  attempt_count       0 until execution begins, 1 after; see attempts below
  claimed_at
  started_at
  finished_at
  result              canonical ToolOutput JSON  (see below)
  result_digest       sha256 of result, for integrity checking
  result_size_bytes
  result_storage_kind inline | artifact_ref     (only `inline` in M10-A)
  error_code          from the classified failure taxonomy
  checkpoint_revision
  recovery_decision   what recovery did with this row, if anything
  journal_version
```

`result` is stored in full, not only as a digest. The most important recoverable window is "the tool succeeded, the journal recorded completion, the checkpoint was never committed"; on resume the runtime must return the **original observation** to the model. If only a digest were kept, the runtime would have to re-execute the tool to recover its output, which defeats the journal.

What is stored is the **canonical `ToolOutput`** — the value the runtime had already normalised and was about to put in the transcript — not the tool's raw payload. Three stages are distinct and must not be conflated:

```
Raw tool result  ->  Canonical ToolOutput  ->  Model-visible observation
```

The dispatcher already bounds and normalises output (it truncates past `max_output_chars`). The journal persists that canonical value **after** normalisation, and recovery replays it verbatim. **Recovery must not re-truncate or otherwise re-normalise it**: if the first run showed the model observation A and recovery showed observation B, the same logical call would have two different meanings. A 20 MB read that the dispatcher reduced to 20 KB is journaled as the 20 KB.

A `MAX_JOURNALED_RESULT_BYTES` ceiling is frozen so an oversized result cannot make the journal unbounded. Exceeding it must **not** silently truncate, because that would break exact transcript reconstruction. M10-A stores `result_storage_kind = inline` only, which is sufficient because the dispatcher already bounds output. When a case needs it, the field allows `artifact_ref` (a path or blob id plus sha256) without another schema change.

Journal results are durable sensitive data. If a `ToolOutput` could ever contain a secret, the answer is for the **tool output contract** to keep secrets out of model-visible output, not for recovery to redact: a redacted journal result could not reconstruct the transcript exactly. Journal retention and trace redaction are therefore different concerns and must not be unified into one policy.

`logical_call_id` and `idempotency_key` are separate fields because they mean different things:

- `logical_call_id` is the harness's identity for the agent's call. It must be stable across resume.
- `idempotency_key` is what a key-honouring tool deduplicates on. It is derived stably (for example `run_id + logical_call_id`) or generated once and persisted. Reriving it differently on resume is forbidden.

This keeps the frozen metric semantics intact: one logical call with one key may later produce several physical attempts, and it is still one `logical_tool_call`.

### State machine

| State | Guarantee |
| --- | --- |
| `claimed` | The runtime accepted this logical call and created the entry. **The tool has not started.** |
| `started` | This state is **durably committed**, and only then may the runtime call the tool. |
| `completed` | The tool returned and the full `ToolOutput` is persisted with the state. |
| `failed` | The failure is known to have left **no uncertain side effect**. |
| `indeterminate` | Whether the side effect occurred cannot be established. |

The ordering is the crux: `claimed` and `started` are written and fsynced **before** `tool.execute` is awaited. Reversing that order makes the crash window unrecoverable.

**The boundary between `failed` and `indeterminate` is side-effect certainty, never the presence of an exception.** Frozen wording:

> `failed` means the runtime has sufficient evidence that recovery cannot duplicate an uncertain side effect. An exception alone is not such evidence.

Two contrasting cases fix the rule:

```
read_only tool -> FileNotFoundError                      -> failed
non_idempotent tool -> begins writing -> RuntimeError    -> indeterminate
```

In the second case the runtime knows an exception occurred and does **not** know how far the write got, so it must not record `failed`. Operationally, execution is what creates uncertainty:

| Execution attempted? | Effect class | Terminal state on failure |
| --- | --- | --- |
| no (schema, policy, unknown tool) | any | `failed` |
| yes | `read_only` | `failed` |
| yes | `idempotent` | `failed` |
| yes | `non_idempotent` | **`indeterminate`** |

Without this rule M10-B will drift into `except Exception: journal.failed(); if recoverable: retry()`, which silently discards the guarantee this ADR exists to provide.

### Recovery decision matrix

Frozen. Recovery reads the journal row and applies this table.

| Journal state | `read_only` | `idempotent` | `non_idempotent` |
| --- | --- | --- | --- |
| `claimed` | execute | execute | execute |
| `started` | re-execute | replay with the **same** `idempotency_key` | **refuse → `indeterminate`** |
| `completed` | reuse stored result | reuse stored result | reuse stored result |
| `failed` | terminate (M10-A) | terminate (M10-A) | terminate (M10-A) |
| `indeterminate` | requires intervention | requires intervention | **requires intervention; never replay** |

M10-A implements no retry, so a `failed` invocation encountered during recovery **terminates the run** rather than being replayed. Which `failed` cases may one day retry is decided in M10-B from `error_code` + `recoverable`; keeping the milestones separate avoids folding retry policy into the journal design.

### The crash window this design exists for

```
1. journal: claimed            (durable)
2. journal: started            (durable commit)   <-- before any side effect
3. tool performs its side effect
4. CRASH                        (completed never written)
```

On resume the journal reads `state = started`, `effect_class = non_idempotent`. The runtime must not call the tool:

```
refuse replay -> indeterminate -> run fails / requires intervention
```

A false-positive `indeterminate` is accepted. If the crash occurred after `started` but before the tool actually did anything, the runtime still fails closed. That is the intended trade: "I cannot confirm whether this happened, so I will not run it again" is preferable to "it probably did not happen, let me retry".

### Recovery must reconstruct the conversation effect, not only skip the call

Avoiding a duplicate side effect is necessary but not sufficient. In the `completed`-but-checkpoint-lost window the checkpoint's message list still lacks the tool result, so recovery must also append the recovered observation as a tool message before continuing. Otherwise the model would see an assistant tool call with no matching result, and the runtime's own contract check (`admitted_calls_were_executed`) would be looking at an inconsistent transcript.

The same applies to `claimed` (nothing was executed yet, so the call proceeds normally) and to `indeterminate` (the run terminates, and the transcript ends without a fabricated tool result).

### Where the journal is written

The **runtime loop** owns journaling, not the dispatcher. The loop is what resumes and what owns `run_id` and the checkpoint revision, and those are the values a journal row must carry. The dispatcher stays responsible for validation, timeout, execution, and failure classification; the loop wraps the dispatch with the durable `claimed`/`started` writes and the terminal state write.

This split matters for behavior: the durable `started` commit must happen after policy approval and immediately before the dispatch call, so a run suspended for approval never writes `started` for a call that was never authorised.

`claimed` is created at the same point — **after policy and approval, immediately before execution begins** — not when the model emits the call. The journal answers "did this side-effect call enter its execution lifecycle?", not "did the model once ask for something", which the trace already records. A suspended or denied call therefore leaves no journal row at all, and the run does not accumulate entries permanently stuck in `claimed`.

`logical_call_id` is still generated when the model emits the action and is carried in the checkpoint and trace; it does not depend on the journal existing.

### How the idempotency key reaches a tool

The key must be available to the tool implementation, since only the tool can deduplicate on it. `ToolContext` gains an optional `idempotency_key` (and the `logical_call_id`), populated by the loop for every dispatch. A tool that does not consume it simply ignores it, which is why a tool that ignores it must not be declared `idempotent`.

### Physical attempts: freeze the abstraction, not the storage

The two levels are frozen **semantically** now, because the metric contract depends on them:

```
Logical Invocation          Physical Attempt
  logical_call_id              attempt_no
  idempotency_key              started_at / finished_at
  effect_class                 error_code
```

But M10-A does **not** create a `tool_attempts` table. Retry is not designed yet, so an attempt record's real fields are unknown — backoff duration, timeout source, retry reason, remaining deadline, the exception, the retry decision, a physical execution id. Creating the table now would guarantee a migration in M10-B.

M10-A therefore keeps only `tool_invocations` with `attempt_count` (0 before execution, 1 after). The ADR records the limit explicitly:

> `attempt_count` is not a physical execution log. When M10-B introduces retry, physical execution history is modelled as separate attempt records.

This yields both goals at once: the invocation identity does not need rework, and no attempt storage schema is frozen prematurely. It also keeps the already-frozen metric contract expressible — one `logical_tool_call` to N `tool_attempts`.

### Trace events

New event types, each carrying `invocation_id`, `logical_call_id`, `tool_name`, `effect_class`, `attempt_no`:

```
tool.invocation.claimed
tool.invocation.started
tool.invocation.completed
tool.invocation.failed
tool.invocation.indeterminate
tool.recovery.decided
```

`tool.recovery.decided` additionally records:

```
decision = reuse_result | replay_read_only | replay_idempotent | refuse_replay
```

So the Step Projection can later render a safe recovery from the trace without inventing state. The trace stays evidence; the journal stays authoritative.

### Migration

No journal is fabricated for existing runs. A run uses the new semantics only when `recovery_semantics_version >= 1`, which is the field already added to evaluation reports in M8-C-prep.

Recovery of a legacy checkpoint (no journal rows) must **fail closed** for any unfinished call that may have had a side effect. The runtime must not reconstruct journal rows by inferring from trace events.

```
recovery_semantics_version:
  0 = pre-journal (no side-effect record; fail closed on unfinished calls)
  1 = journal + effect-class recovery
```

## Consequences

A resumed run can now answer "did this side effect happen, and what did it return?" from durable state, and it refuses to guess. Completed calls are never re-executed; `started` non-idempotent calls are never automatically replayed.

The delivery guarantee is **not** blanket at-most-once, and the documentation must not describe it that way. Precisely:

> For non-idempotent side effects the runtime uses fail-closed recovery: it will not automatically repeat an operation whose completion is unknown. For read-only tools, and for tools that genuinely honour a stable idempotency key, safe replay is allowed.

Strict delivery-semantic claims are easy to challenge in review; the honest statement is the one above.

Costs and residual limits, to be recorded rather than hidden:

- Journal writes add a synchronous durable commit before every tool call. That is the price of a recoverable side-effect record.
- False-positive `indeterminate` is expected and accepted.
- `indeterminate` requires an intervention path. M10-A may surface it as a failed run with an explicit error; a richer resumption API is not in scope.
- A tool that is mis-declared `read_only` while having a side effect breaks the guarantee. Declaration accuracy is a review responsibility, and the default is the safe one.

## Verification plan (this change defines it; M10-A implements it)

Four fault-injection points, each a test that kills the run at that point and resumes:

| Injection point | Expected on resume |
| --- | --- |
| `after_claim` | `claimed` → execute once |
| `after_started_before_dispatch` | `read_only`/`idempotent` proceed per table; `non_idempotent` → `indeterminate` |
| `after_tool_return_before_completed_commit` | `started` → `indeterminate` for `non_idempotent` (the hard case) |
| `after_completed_before_checkpoint` | reuse stored result; do not re-execute |

The two load-bearing tests:

**Test A — completed but checkpoint lost.** A `non_idempotent` tool executes once; the journal records `completed`; the process dies before the checkpoint commits; resume. Assert: the tool's execution count is still **1**, the original `ToolOutput` is recovered from the journal, and the tool is not called again.

**Test B — the `started` crash window**, split into its two physical realities. The journal cannot observe the exact moment a side effect occurs inside a tool, so both cases are indistinguishable at `started`:

- **B1 — `started` committed, crash before the tool produced any side effect.** On recovery the journal still reads `started`, so the runtime refuses to replay even though a replay would have been harmless. This is a deliberate **false positive**: availability is traded for safety.
- **B2 — `started` committed, the side effect occurred, crash before `completed`.** Recovery reads the same `started` and refuses, which is what actually prevents a duplicated side effect.

Both assert: the recorded execution count is still **1**, the runtime does not call the tool again, the journal moves to `indeterminate`, and the run does **not** report `SUCCEEDED`.

Test B2 is the most valuable evidence in M10-A; it is the case the pre-journal design cannot handle at all. Test B1 is what makes the reasoning complete: it shows the design is intentionally conservative about a window it cannot observe, rather than accidentally correct.

## Exit criteria for M10-A

Design is accepted when these are answered unambiguously; implementation begins after that.

1. What is the source of truth for side-effect recovery? — the Invocation Journal.
2. What do `claimed` / `started` / `completed` / `indeterminate` each guarantee? — the state table above.
3. How does each effect class recover? — the decision matrix above.
4. How are logical calls and physical attempts distinguished? — `tool_invocations` vs `tool_attempts`.
5. When is replay allowed? — read-only always; idempotent with the same key; never for `non_idempotent` after `started`.
6. When must recovery fail closed? — `non_idempotent` at `started`/`indeterminate`, and any legacy run without journal rows.
7. How is the original observation recovered after `completed`? — the full `ToolOutput` is stored in the journal row.
8. How is the `started` crash window proven not to duplicate a side effect? — Test B: a counted side effect that must remain at 1 execution.

## Follow-ups implemented after this ADR

- **M10-B**: the retry decision matrix (`runtime/retry.py`), `tool_attempts` for
  physical executions, deterministic backoff, and per-tool `ToolSpec.timeout_seconds`.
- **M10-C**: `RunBudget.max_wall_seconds`, a whole-run ceiling checked **between
  steps** and deliberately never by cancelling an in-flight tool — aborting a side
  effect mid-write would produce exactly the ambiguous state this ADR exists to
  prevent. A run that passes its deadline fails with `error_type=deadline_exceeded`.

## Out of scope

- Retry policy *beyond* the decision matrix: adaptive retry, jitter, retry queues, background scheduling. M10-B implemented the matrix itself plus bounded deterministic backoff.
- Circuit breakers, distributed leases, and a `DEAD` run state: no breakable object exists in a single-process local runtime.
- Exactly-once delivery. It is not achievable across the `started` window, and claiming it would be a false invariant.

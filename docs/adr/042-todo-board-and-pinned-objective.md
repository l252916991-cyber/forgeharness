# ADR 042: A mutable todo board, and a task objective that compaction cannot elide

Date: 2026-10-30. Status: implemented.

## Context

`runtime/plan_execute.py` writes an `ExecutionPlan` before the first model call and
never revisits it. The plan is a `FrozenModel` with 1-20 steps, so an assumption that
turns out to be wrong mid-run has nowhere to be recorded: the model can say "I'll
adjust the plan" in prose, and nothing outside the transcript changes.

That would be survivable if the transcript were permanent. It is not. `context/assembly.py`
folds older turns into one summary under budget pressure, and the first message it folds
is the first user message -- the task objective. The failure this produces is not a crash.
The run keeps making tool calls, keeps reporting progress, and arrives at a verifier that
correctly checks whether the final answer is *supported*. A verifier answers "is this
answer backed by evidence", never "is this the task you were given". So the run finishes
confidently on a different problem.

Two mechanisms are needed, and neither is sufficient alone: a durable record of intent
that is not the transcript, and a guarantee that the objective and that record are never
folded away.

## Decision

`runtime/todos.py` introduces a revisioned `TodoBoard` owned by the runtime:

- `TodoItem` is `(id, content, status)` with `status` in pending / in_progress /
  completed, at most one in_progress, at most 50 items, unique ids. A board with three
  "current" tasks cannot tell the model which one its next step belongs to.
- `replace()` increments the revision only when the content changes, which is what makes
  the single `write_todos` tool honestly `IDEMPOTENT` and safe to replay on the
  at-least-once dispatch path.
- `WriteTodosTool` replaces the whole board. Whole-board replacement rather than
  per-item patching keeps the contract small enough for a model to get right, and makes
  the write naturally idempotent. A malformed board returns as an observation, not a
  run-ending fault: it is the model's mistake to fix.
- `replay_board(messages, evidence=...)` reconstructs the board chronologically from
  correlated successful tool outputs. An assistant proposal alone is never authority:
  denied, failed and unexecuted proposals cannot complete work on resume. Each accepted
  result must match the proposed content, canonical rendered observation, expected
  revision and structured output metadata (`todos`, digest and counts). No-op writes
  preserve the revision; clearing a nonempty board increments it. A resumed runtime
  restores that exact revision, including after a completed journal result is reused.
  The existing checkpoint's `ToolEvidence` retains the full successful output when its
  model-visible observation was compressed. Missing or contradictory success evidence
  refuses recovery instead of silently clearing the board. The messages-only helper
  accepts only exact canonical successful observations and cannot restore compressed
  observations without their full evidence.

`AgentRuntime` gains three parameters: `todos` (a board, or `None` for callers that do not
want the feature), `max_todo_rejections` (default 3) and the registration of
`write_todos` when a board is present.

The finish gate sits in front of the verifier, not beside it:

- A final action with any open item appends a user-role rejection naming the open items
  and continues the loop, traced as `todo.final_rejected`.
- Persisting past `max_todo_rejections` fails the run with `unfinished_todos: ...` rather
  than looping until the step budget runs out, because a model that will not close its
  board is a defect worth reporting as one.

`context/assembly.py` gains two pins:

- `pin_task=True` makes the first user message a *required unit*. `required` means
  "survives verbatim", not "everything": with the objective pinned, the turns after it
  stay compactable. Units are ordered by position when reassembled, so the objective is
  sent before the summary that replaced the turns after it.
- `pinned` is caller state, re-read from a callable on every request (the board changes
  between turns; passing it per request would let one call site forget it). Pinned
  messages are injected directly after leading system instructions and must be SYSTEM
  messages: pinned state speaks with the harness' authority, and a user-role pin would
  read as the operator talking.

An empty board pins nothing -- a run that never planned must not pay a per-request header
announcing that it has no plan.

## Consequences

Every run that enables todos pays for the rendered board on every request. `render_todos`
is bounded at 4,000 characters and truncates the tail of a long board rather than the
header, so the cost is bounded and the state stays inspectable.

`write_todos` is `risk=READ`: it changes harness state only, so it needs no approval
round-trip. That is a deliberate asymmetry with filesystem tools, and it is safe only
because the board is bounded and has no external effect.

The board is per-run. `run()` resets it and both resume paths restore it from the
transcript, so one task cannot inherit another's plan.

# Durable scoped sessions

`SessionService` is an embeddable, host-authorized conversation layer over the existing public `Harness` and `WorkflowRunner` controllers: it persists scoped conversations, ordered turns, stable request keys and run bindings, re-checks current host permissions for every operation, and executes each turn through those controllers. It is introduced in the 0.4.0 candidate; SDK API v1 is preserved. The design record is [ADR 014](adr/014-durable-scoped-sessions.md); the phase scope and its keyless acceptance script are in [SESSION_PHASE_SCOPE.md](SESSION_PHASE_SCOPE.md).

Qualification of the frozen candidate is bound to `reports/session-release-audit.json`, which records the frozen source candidate and the gate evidence produced for it. This document qualifies nothing, asserts no gate outcome, and cannot replace that report or the phase script. Everything described here is synthetic and local: real-model answer quality, legal or commerce business quality, professional judgement and production readiness are out of scope.

## Host contract

```python
SessionService(
    application: ApplicationSpec | WorkflowSpec, *, state_dir: Path, authorize: Authorize,
    validate_materials: ValidateMaterials | None = None, max_message_chars: int = 100_000,
    history_limit: int = 20,
)
```

| Argument | Required | Contract |
| --- | --- | --- |
| `application` | yes | Registered `ApplicationSpec`, or a `WorkflowSpec` whose step applications are executed. Its `id`, `version` and `policy_version` must match every accepted context. |
| `state_dir` | yes | Local owner-private directory (see [Storage](#storage)); created with mode `0700`, and group/other permission bits are refused. |
| `authorize` | yes | `(RunContext, SessionOperation, Session \| None) -> bool`; must query *current* host permissions for that operation. Only the literal `True` grants access. |
| `validate_materials` | when the spec, or any step application, declares `context_providers` | `(RunContext, tuple[ContextMaterial, ...]) -> bool`; must check each concrete source version and current access. Only the literal `True` accepts. |
| `max_message_chars`, `history_limit` | no | Defaults and bounds as in the signature; accepted ranges 1–1 000 000 and 1–100. |

**Authorization.** `authorize` receives the authenticated `RunContext`, the `SessionOperation` attempted, and the stored `Session` (`None` for `create`), and it runs again on every read, so revocation is enforced on the next call. A session is bound to application id and version, policy version, tenant, owner actor, resources and scope; a context differing on session id, application, version, policy version, scope, or — outside delegated approval — actor id is refused as `SessionUnavailable`. `approve`, and `resume` carrying a grant, also authorize a second principal, the approver, under `approve` and the same rules apart from owner-actor equality; that principal must come from a separate host authentication. The service performs **no authentication of its own**: it reads no credentials and takes no identity from message text, and a callback returning anything other than `True` fails closed. A context changed by the evidence transform (for example a credential in a correlation id) is refused before any session is touched.

**Material validation.** The service checks scope itself: matching `application_id`, matching tenant, a `resource_id` inside the context scope, and no `expires_at` in the past. The host callback must then confirm each concrete source version and current access. Material failing either check is refused before execution, and recorded material is revalidated on every read, when history is assembled and before resume. A `Harness` turn that fails preparation with no durable evidence is recorded as `failed` with `error="execution_preparation_failed"` instead of being left looking unfinished; a workflow turn that raises keeps the suspended status and node error reported by `WorkflowRunner`, and a workflow turn with no durable workflow state is recorded as `failed` the same way.

Construction rules:

| Condition | Result |
| --- | --- |
| `authorize` is not callable | `ValueError` (`a live host authorizer is required`) |
| `max_message_chars` outside 1–1 000 000, or `history_limit` outside 1–100 | `ValueError` (`invalid message or history limit`) |
| Context providers declared without `validate_materials` | `ValueError` (`context providers require current material validation`) |
| `state_dir` accessible to group or others | `ScopeError` (`session directory must be owner-private (mode 0700)`) |
| Existing session schema version other than 1 | `SessionConflict` (`unsupported session schema`) |

## Public operations

Every method takes the authenticated `RunContext` first; `SessionOperation` is the closed vocabulary `create`, `read`, `submit`, `close`, `inspect`, `events`, `approve`, `resume`, `delete`.

| Method | Other arguments | Authorizes | Returns |
| --- | --- | --- | --- |
| `create(context)` | — | `create` | `Session` |
| `get(context)` | — | `read` | `Session` |
| `history(context, ...)` | `limit=20`, `before_sequence=None` | `read` | `tuple[SessionTurn, ...]` |
| `get_turn(context, turn_id)` | `turn_id` | `read` | `SessionTurn` |
| `submit(context, ...)` (awaitable) | `request_id`, `text` | `submit` | `SessionTurn` |
| `inspect(context, turn_id)` | `turn_id` | `inspect` | `RunResult` or `WorkflowResult` |
| `events(context, turn_id, ...)` | `turn_id`, `after=0`, `limit=100` | `events` | `tuple[dict[str, Any], ...]` |
| `approve(context, turn_id, ...)` | `turn_id`, `approver` | `approve` for owner and approver | `ApprovalGrant` |
| `resume(context, turn_id, ...)` (awaitable) | `turn_id`, `grant=None`, `approver=None` | `resume`, plus `approve` for the approver when a grant is given | `SessionTurn` |
| `close(context)` | — | `close` | `Session` |
| `delete_turn(context, turn_id)` | `turn_id` | `delete` | `None` |
| `delete(context)` | — | `delete` (also permitted on an already deleted session) | `None` |

Bounded arguments fail as `ValueError`: an empty message or one over `max_message_chars`; a `request_id` that is empty, over 200 characters or changed by the evidence transform; a `history` limit outside 1–100 or a non-positive `before_sequence`; and event pagination outside `after >= 0` and `1 <= limit <= 1000`.

## Guarantees

- **Scope binding.** A session records application id and version, policy version, tenant, owner actor, resources and scope; cross-binding, cross-tenant and post-revocation access is refused rather than filtered, and binding compatibility is re-established on every operation, so a changed application or policy version is never silently adopted.
- **Identity.** Identity never comes from request text: the host supplies the `RunContext`, the service validates it against the registered spec, and message text becomes only the task. The HTTP adapter additionally rejects `actor`, `scope`, `tenant_id`, `roles` or `session_id` in any request body.
- **Single writer, one active turn.** Writable operations hold a non-blocking `flock` on a per-session lock file, and a second worker is refused (`session has an active worker`) rather than queued or interleaved. At most one turn is active: while a turn is `starting`, `running` or `awaiting_approval`, a new submission is refused.
- **Stable request keys.** A turn id derives from the session key and the `request_id`; the stored digest covers the stripped, transformed text and the owner actor, and the database allows one row per session and request key. Replaying an identical request returns the stored turn after reconciliation — no second run, no second model call — while reuse with different text is refused, including after a tombstone.
- **Crash reconciliation.** An active turn is reconciled against durable controller evidence: a stored completed result is copied into the turn, otherwise the stored state is kept, so a turn that cannot be confirmed is never reported as success. A turn that never started is not invented (`execution has not started`), and `resume` starts it only while it is still `starting` and no grant is presented. A `succeeded` turn whose evidence went missing is refused rather than replayed. Workflow turns use the non-nullable `WorkflowRunner` lookup, so an unknown, denied or tombstoned workflow is refused rather than restarted.
- **History.** Only `succeeded` turns enter the window; failed, exhausted and awaiting-approval turns contribute no context. The window is bounded by `history_limit`, each entry is revalidated before use, and history reaches the model as untrusted `ContextMaterial` marked `untrusted_conversation_history`, versioned per turn from its text, output and run id. The material actually sent to the model remains authoritative in the trace (`model.request`); this layer adds no summarisation, extraction or long-term memory.
- **Delegated, single-use approval.** The owner requests approval and a separately authenticated approver decides; both principals are authorized against current permissions, and the policy's own approver check still applies. Grants are single-use: a consumed, forged or expired grant is rejected by the durable ledger, and a grant for a turn that is no longer active is refused, so a replay is never acknowledged as a fresh approval. Recovery reuses the original binding, and changed owner roles refuse resume (`owner roles changed; start a new execution`).
- **Deletion.** Deleting a turn writes a tombstone first, clears its text, output, error, materials and history links, then deletes the controller-side evidence; the request key can never be replayed. Deleting a session tombstones it, deletes its turns, and clears local copies with SQLite `secure_delete`, a WAL truncate checkpoint and `VACUUM`. Deleted identities are retired permanently: recreating the session is refused, and deleting twice is idempotent rather than a resurrection.
- **Close.** A session closes only once no turn is active; an unresolved turn blocks closing after a reconciliation attempt. Closing blocks submissions, approvals and resumptions (`session is closed`); the session stays readable and can still be deleted.
- **Distinguishable states.** Session status is `open`, `closed` or `deleted`; turn status is `starting`, `deleted` or a `RunStatus` value (`created`, `running`, `awaiting_approval`, `succeeded`, `failed`, `exhausted`, `cancelled`), with `starting`, `running` and `awaiting_approval` active. Failure, exhaustion, awaiting approval and success are therefore separate, queryable states, and a suspended turn is never presented as a successful one.

## Storage

- The database is `<state_dir>/sessions.sqlite`, opened in WAL mode with `BEGIN IMMEDIATE` transactions. It stores no credentials beyond what the host put in the context, and no encryption at rest is claimed.
- `session_schema(version)` holds an independent session schema version, currently `1`, unrelated to the legacy knowledge tables: no legacy migration is performed or implied, and old knowledge data is neither read, re-authorized nor copied.
- `sdk_sessions(id TEXT PRIMARY KEY, payload TEXT)` stores JSON `Session` payloads; `sdk_turns(session_key, id, sequence, request_id, digest, payload)` stores JSON `SessionTurn` payloads, unique on `(session_key, id)`, `(session_key, sequence)` and `(session_key, request_id)`.
- Row keys are SHA-256 digests of the application, tenant and session identifiers (and of the request id), so raw identifiers are not used as file names.
- Each session executes in `<state_dir>/executions/<session key>` and locks `<state_dir>/<session key>.lock`; run evidence lives there, not in the session tables, and the two SQLite databases are not joined by a distributed transaction.

## Optional HTTP adapter

`session_router(service, *, authenticate, authenticate_approver=None)` builds an `APIRouter` with the `/harness` prefix. FastAPI is imported only by that module and comes from the `web` extra, not from the base SDK.

- `authenticate(Request) -> Awaitable[RunContext]` authenticates the caller on every request and must authorize the owner context and the selected session from current host permissions. It must **not** build a trusted context from request JSON: path identifiers select objects, not scope, and `{session_id}` must equal the authenticated `context.session_id`.
- `authenticate_approver(Request) -> Awaitable[RunContext]` supplies the separately authenticated approver; without it the owner context is reused and approval is self-approval. Both callbacks run again when a grant is consumed, and a submitted grant is untrusted evidence validated by the durable ledger.
- Session routes: `POST /harness/sessions` → **201** (optional empty body, identity fields rejected); `GET /harness/sessions/{session_id}` → **200**; `GET /harness/sessions/{session_id}/history` → **200** (`limit` 1–100, `before_sequence` ≥ 1); `POST /harness/sessions/{session_id}/messages` → **200** (`request_id` 1–200, `text` 1–100 000 chars).
- Turn routes: `GET .../turns/{turn_id}` → **200**; `GET .../turns/{turn_id}/inspect` → **200**; `GET .../turns/{turn_id}/events` → **200** (`after` ≥ 0, `limit` 1–1000); `POST .../turns/{turn_id}/approve` → **200** (returns the grant); `POST .../turns/{turn_id}/resume` → **200** (optional `grant`).
- Session actions: `POST /harness/sessions/{session_id}/close` → **200**; `DELETE .../turns/{turn_id}` → **204**; `DELETE /harness/sessions/{session_id}` → **204**.

Failures: **401** is raised by the host callbacks themselves and never invented by the router; **403** maps `ScopeError` (including `SessionUnavailable`) and `ApprovalError` to one fixed detail (`session operation unavailable or unauthorized`) that does not disclose whether an identifier belongs to another tenant or actor; **409** maps `SessionConflict` and `CheckpointConflict` with the conflict text; **422** maps `ValueError` from the service and request validation, including unknown identity fields, missing or oversized fields and out-of-range query parameters.

## Minimal example

`SessionService` is exported from `forgeharness.sdk`. The authorizer and material validator below are host placeholders — a real host queries current permissions and current source versions on every call, and only an isolated synthetic example may answer `True` unconditionally. `examples/session_demo.py` is the complete keyless client that builds the synthetic application, contexts, provider and deterministic model.

```python
import asyncio
from pathlib import Path

from forgeharness.sdk import (
    Actor,
    ResourceScope,
    RunContext,
    RunStatus,
    Session,
    SessionOperation,
    SessionService,
)


def authorize(context, operation: SessionOperation, session: Session | None) -> bool:
    return True  # host placeholder: query current permissions per operation


def validate_materials(context, materials) -> bool:
    return True  # host placeholder: check each concrete source version and access


async def main(spec, state_dir: Path) -> None:
    owner = RunContext(
        application_id="notes",
        application_version="1",
        policy_version="1",
        run_id="request",
        session_id="s1",
        correlation_id="request",
        actor=Actor(id="owner", roles=("use",)),
        scope=ResourceScope(tenant_id="tenant", resources=("note:1",)),
    )
    reviewer = owner.model_copy(update={"actor": Actor(id="reviewer", roles=("approve",))})
    service = SessionService(
        spec, state_dir=state_dir, authorize=authorize, validate_materials=validate_materials
    )
    service.create(owner)  # bind a new session
    turn = await service.submit(owner, request_id="turn-1", text="Write the note")
    if turn.status is RunStatus.AWAITING_APPROVAL:  # write is suspended
        grant = service.approve(owner, turn.id, approver=reviewer)  # separate approver
        turn = await service.resume(owner, turn.id, grant=grant, approver=reviewer)
    print(turn.status, turn.output)  # succeeded / failed / exhausted / awaiting_approval
    service.close(owner)  # close the session


# spec is the host's registered ApplicationSpec or WorkflowSpec.
asyncio.run(main(spec, Path("state/sessions")))
```

## Limits and non-goals

- Single-machine, POSIX and local SQLite only: the writer lock is `fcntl` based, with no distributed lease, network filesystem support or general exactly-once claim.
- No cross-database transaction: session state and controller run evidence are separate SQLite databases under `state_dir`, reconciled explicitly rather than atomically.
- No built-in IAM, account directory, cookie handling, CSRF policy or rate limiting. `authorize` and `validate_materials` are host callbacks; the adapter is a routing layer, not an authentication server, and no multi-tenant server isolation, sandboxing or encryption at rest is claimed.
- No legacy knowledge migration and no automatic re-authorization of old data.
- No automatic long-term memory: history is a bounded, untrusted, revalidated window of completed turns, not a memory store or a conversation summary.
- Deletion covers the session's local copies and the controller evidence it manages; it cannot reach host-side backups, attachments, exports or domain records, and it is not a secure-erase guarantee for the storage device.
- Application-level rules — roles, approval policy, budgets, business validation and domain semantics — stay in the application and the host, as described in [SDK_GUIDE.md](SDK_GUIDE.md).

The phase acceptance script and the synthetic legal/commerce scope are in [SESSION_PHASE_SCOPE.md](SESSION_PHASE_SCOPE.md); the rationale, alternatives and consequences of these choices are recorded in [ADR 014](adr/014-durable-scoped-sessions.md).

# 050 — A definitive provider refusal settles as a failure, not as an unknown outcome

Date: 2026-11-08. Status: accepted.

## Context

In the long-context scaling study (`reports/long-context-scaling/REPORT.md`) the
provider account ran out of balance midway and every later request was answered with
HTTP 402. The two durable paths disagreed about what that meant:

- the single-call path (`RunKernel`, ReAct) reported the run as failed;
- R2 collaboration nodes, sequential and parallel, reported
  `needs_reconciliation`, kept the node's budget reservation and, in the parallel
  runner, left the provider capacity slot unresolved.

R2 treats every model exception after the durable sending marker as an unknown
outcome, because a lost connection or a server error can follow real inference that
was billed. A 402 is different: the response itself proves the request was refused
before any inference. Holding it for reconciliation asks an operator to resolve a call
whose outcome is already known.

## Decision

- `forgeharness.models.base.ModelRequestRejected(status_code)` (exported from the SDK)
  names a definitive refusal. `is_definitive_rejection` accepts HTTP 4xx except 408
  (the server gave up waiting) and 429 (throttling a later attempt may pass); the
  constructor refuses any other status.
- `OpenAICompatibleModel` raises it for those statuses instead of a generic HTTP error.
  The provider's response body is not kept: it is untrusted and may echo the request.
  Other HTTP errors are raised exactly as before.
- `dispatch_model_once` records `status_code` on the `model.failed` trace event for a
  refusal only, and marks the outcome `rejected` with `response_received`.
- Both R2 runners settle a refused node as `failed`, with output
  `{"error": "provider_rejected", "status_code": …}`, zero usage, a released
  reservation and the error `provider rejected the request (HTTP …)`. The parallel
  runner confirms the capacity ticket ended. A failed node blocks its dependants as
  any failed node does.
- 5xx, 408, 429, timeouts and any other exception keep today's meaning: unknown, held
  for reconciliation, never re-sent.

## Consequences

- An account or request problem now stops a collaboration run with a clear failure
  instead of a reconciliation queue. A settled refusal is never re-sent on resume.
- No recovery-version change: an existing `unknown` node is reconciled as before, and
  the new outcome is an ordinary failed node. Checkpoints written before this change
  keep their state.
- A custom `Model` adapter opts in by raising `ModelRequestRejected` for statuses it
  knows to be definitive; adapters that do not are treated conservatively, as today.
- 429 still stops an R2 node as unknown. Making throttling retryable is a separate
  decision about which attempts a durable run makes.

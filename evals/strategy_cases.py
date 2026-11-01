"""Synthetic development-diagnosis tasks, with disjoint dev/evaluation templates.

These short tasks are not repository repair or a public benchmark. No submitted
code is executed. Expected answers stay in the external verifier, never prompts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    question: str
    materials: tuple[str, ...]
    answer: str


DEV = (
    Case(
        "dev-p1",
        "parallel",
        "Return the failing modules, alphabetically, comma-separated.",
        (
            "auth: permission checked after write; invariant: check before write.",
            "billing: amount is integer cents as required.",
            "trace: saves raw api_key; invariant: never persist credentials.",
        ),
        "auth,trace",
    ),
    Case(
        "dev-p2",
        "parallel",
        "Which checks pass? Return sorted IDs comma-separated.",
        (
            "a: expected HTTP 403, actual 403.",
            "b: expected one receipt, actual two.",
            "c: expected terminal succeeded, actual succeeded.",
        ),
        "a,c",
    ),
    Case(
        "dev-p3",
        "parallel",
        "Find the highest severity module: p0 is higher than p1 or p2.",
        ("cache is p2.", "router is p1.", "ledger is p0."),
        "ledger",
    ),
    Case(
        "dev-p4",
        "parallel",
        "Return modules requiring a database migration, sorted.",
        (
            "alpha only changes documentation.",
            "beta adds a durable table.",
            "gamma adds a nullable stored column.",
        ),
        "beta,gamma",
    ),
    Case(
        "dev-c1",
        "chain",
        "Return the final state after the events.",
        (
            "Start queued. start: queued -> running. finish: running -> succeeded.",
            "Events in order: start, finish.",
        ),
        "succeeded",
    ),
    Case(
        "dev-c2",
        "chain",
        "Return remaining budget as a decimal integer.",
        (
            "Initial budget 20. Reserve 8. Settlement releases reservation then charges actual 5.",
            "After settlement charge a further 4.",
        ),
        "11",
    ),
    Case(
        "dev-c3",
        "chain",
        "Return the final artifact version.",
        ("Input source is v2. Build maps v2 -> artifact b7.", "Review of b7 produces release r9."),
        "r9",
    ),
    Case(
        "dev-c4",
        "chain",
        "Return the admitted node IDs in execution order.",
        (
            "a has no dependencies. b depends on a. c depends on b.",
            "All nodes succeed; scheduler runs only nodes with completed dependencies.",
        ),
        "a,b,c",
    ),
    Case(
        "dev-e1",
        "error",
        "Choose retry, reconcile, or fail.",
        (
            "A non-idempotent payment was dispatched. Response was lost.",
            "No authoritative receipt is currently available.",
        ),
        "reconcile",
    ),
    Case(
        "dev-e2",
        "error",
        "Choose execute or request_approval.",
        (
            "Approval binds content digest x. Current content digest is y.",
            "An approval may authorize only its exact approved digest.",
        ),
        "request_approval",
    ),
    Case(
        "dev-e3",
        "error",
        "Choose execute or deny.",
        (
            "Caller was authorized at submission; caller is revoked before tool admission.",
            "Policy requires current authorization at each admission.",
        ),
        "deny",
    ),
    Case(
        "dev-e4",
        "error",
        "Choose answer or clarify.",
        (
            "Request: delete the old record. There are two equally old records.",
            "Deletion requires one unambiguous record ID.",
        ),
        "clarify",
    ),
)

# Each evaluation item introduces a different specification/template from DEV.
# Author-generated and inspected during construction: not an unseen external set.
EVALUATION = (
    Case(
        "eval-p01",
        "parallel",
        "Return the module whose license blocks the release.",
        (
            "Module prism: MIT. Allowed licenses: MIT, Apache-2.0.",
            "Module quartz: GPL-3.0-only. Allowed licenses: MIT, Apache-2.0.",
            "Module river: Apache-2.0. Allowed licenses: MIT, Apache-2.0.",
        ),
        "quartz",
    ),
    Case(
        "eval-p02",
        "parallel",
        "Return the endpoint leaking another tenant's resource.",
        (
            "/orders checks owner tenant == actor tenant.",
            "/files looks up by global ID without a tenant predicate.",
            "/notes queries WHERE tenant_id = actor.tenant_id.",
        ),
        "/files",
    ),
    Case(
        "eval-p03",
        "parallel",
        "Return the test that detects a genuine boundary defect.",
        (
            "test_a: limit 5 accepts consumption 5, as specified.",
            "test_b: limit 5 accepts consumption 6, but maximum is inclusive 5.",
            "test_c: limit 5 rejects consumption 7, as specified.",
        ),
        "test_b",
    ),
    Case(
        "eval-p04",
        "parallel",
        "Return the service eligible for cache reuse.",
        (
            "oak: cached revision v1, current revision v2.",
            "pine: cached revision v3, current revision v3, permission valid.",
            "elm: cached revision v4, current revision v4, permission revoked.",
        ),
        "pine",
    ),
    Case(
        "eval-p05",
        "parallel",
        "Return sorted modules violating the no-core-to-domain-import rule.",
        (
            "api/core.py imports domain.legal.",
            "sdk/core.py imports json.",
            "runtime/core.py imports domain.commerce.",
        ),
        "api,runtime",
    ),
    Case(
        "eval-p06",
        "parallel",
        "Return the schema change compatible with an old required-field reader.",
        (
            "change_a removes the required status field.",
            "change_b adds an optional note field; preserves all old fields and meanings.",
            "change_c changes required integer counter to an object.",
        ),
        "change_b",
    ),
    Case(
        "eval-p07",
        "parallel",
        "Which implementation preserves sorted unique IDs? Return its ID.",
        (
            "impl_a: list(set(ids)); order is unspecified.",
            "impl_b: sorted(set(ids)).",
            "impl_c: sorted(ids); duplicates remain.",
        ),
        "impl_b",
    ),
    Case(
        "eval-p08",
        "parallel",
        "Return the log entry permitted by the privacy policy.",
        (
            "entry_a stores the bearer token verbatim; forbidden.",
            "entry_b stores a credential-free request digest; allowed.",
            "entry_c stores the database password verbatim; forbidden.",
        ),
        "entry_b",
    ),
    Case(
        "eval-p09",
        "parallel",
        "Return the worker configuration that prevents duplicate row claims.",
        (
            "config_a selects then updates in separate unguarded transactions.",
            "config_b reads rows without claiming them.",
            "config_c uses an atomic conditional UPDATE and checks affected-row count.",
        ),
        "config_c",
    ),
    Case(
        "eval-p10",
        "parallel",
        "Return the function that checks a Python list for emptiness correctly.",
        ("f_a: len(items) < 0.", "f_b: not items.", "f_c: items is []."),
        "f_b",
    ),
    Case(
        "eval-c01",
        "chain",
        "Return the total physical invocation count.",
        (
            "Logical call x has attempts x1 failed, x2 failed, x3 succeeded.",
            "Logical call y has attempt y1 succeeded. Count attempts, not logical calls.",
        ),
        "4",
    ),
    Case(
        "eval-c02",
        "chain",
        "Return the final HTTP status.",
        (
            "Domain exception SourceGone maps to ResourceUnavailable.",
            "ResourceUnavailable maps to HTTP 410. Other exceptions map to 500.",
        ),
        "410",
    ),
    Case(
        "eval-c03",
        "chain",
        "Return the earliest node whose output becomes invalid.",
        (
            "graph: retrieve -> summarize -> publish. retrieve depends on source rev 4.",
            "Source changes to rev 5. Invalidate directly affected outputs before descendants.",
        ),
        "retrieve",
    ),
    Case(
        "eval-c04",
        "chain",
        "Return the surviving committed rows after recovery.",
        (
            "Transaction T inserts draft and receipt, then crashes before COMMIT.",
            "Before T there were 2 drafts and 2 receipts. SQLite rolls back uncommitted changes.",
            "Format: drafts,receipts.",
        ),
        "2,2",
    ),
    Case(
        "eval-c05",
        "chain",
        "Return the final outbox consumer balance.",
        (
            "Start balance 0. Event e1 adds 7. Event e2 subtracts 2.",
            "Delivery: e1,e2,e1. Consumer deduplicates by event ID.",
        ),
        "5",
    ),
    Case(
        "eval-c06",
        "chain",
        "Return the critical path duration in seconds.",
        (
            "A takes 2 seconds. B and C start after A, taking 5 and 3 seconds in parallel.",
            "D starts after both B and C and takes 4 seconds. No overhead.",
        ),
        "11",
    ),
    Case(
        "eval-c07",
        "chain",
        "Return the authorization result: allow or deny.",
        (
            "Access requires active user AND tenant match AND resource grant.",
            "User active=true; tenant matches=true; resource grant=false.",
        ),
        "deny",
    ),
    Case(
        "eval-c08",
        "chain",
        "Return the final stock count.",
        (
            "Stock starts 10. Request k1 reserves 3; request k2 reserves 2.",
            "Request k1 repeats; idempotency prevents a second reservation. Then k2 is refunded.",
        ),
        "7",
    ),
    Case(
        "eval-c09",
        "chain",
        "Return the number of records after the specified pipeline.",
        (
            "Inputs: [a(active), b(deleted), a(active), c(active)]. First remove deleted.",
            "Then deduplicate by ID. Then keep the first two records.",
        ),
        "2",
    ),
    Case(
        "eval-c10",
        "chain",
        "Return when the next retry is admitted, in milliseconds after start.",
        (
            "Attempt 1 fails at t=10ms; backoff before attempt 2 is 100ms.",
            "Attempt 2 runs 20ms and fails. Backoff before attempt 3 doubles to 200ms.",
        ),
        "330",
    ),
    Case(
        "eval-e01",
        "error",
        "Choose reuse or reject.",
        (
            "An idempotency key already binds payload hash a. New request uses hash b.",
            "The contract forbids reusing a key with different content.",
        ),
        "reject",
    ),
    Case(
        "eval-e02",
        "error",
        "Choose estimate_zero or retain_reservation.",
        (
            "Provider accepted a model request but the response contains no usage.",
            "The budget contract requires authoritative actual usage for settlement.",
        ),
        "retain_reservation",
    ),
    Case(
        "eval-e03",
        "error",
        "Choose publish or wait.",
        (
            "Join requires all dependencies completed. Left branch completed. Right branch failed.",
            "There is no configured partial-result publication mode.",
        ),
        "wait",
    ),
    Case(
        "eval-e04",
        "error",
        "Choose retry_read or reconcile_write.",
        (
            "A read-only lookup timed out, with no side effects and known zero cost.",
            "One safe retry remains under the attempt and wall-clock budgets.",
        ),
        "retry_read",
    ),
    Case(
        "eval-e05",
        "error",
        "Choose continue or stop.",
        (
            "A run's persisted deadline is 12:00 UTC. Current time is 12:01 UTC.",
            "Restarting a process must not renew the deadline. The next node has not started.",
        ),
        "stop",
    ),
    Case(
        "eval-e06",
        "error",
        "Choose discard_receipt or preserve_receipt.",
        (
            "Cancellation arrived while an admitted external write was executing.",
            "The external system confirms the write committed. Cancellation cannot undo it.",
        ),
        "preserve_receipt",
    ),
    Case(
        "eval-e07",
        "error",
        "Choose loop or fail_bounded.",
        (
            "The model returned an illegal tool-call structure on all 3 permitted attempts.",
            "The violation retry allowance is now exhausted.",
        ),
        "fail_bounded",
    ),
    Case(
        "eval-e08",
        "error",
        "Choose accept or reject.",
        (
            "A page's stored SHA-256 describes text A, but current page text is B.",
            "A citation refers to the page. Verification requires a matching content digest.",
        ),
        "reject",
    ),
    Case(
        "eval-e09",
        "error",
        "Choose steal_owner or refuse_takeover.",
        (
            "Another live process holds the local flock and is blocked on a tool.",
            "The single-machine runtime cannot steal an active owner's lock.",
        ),
        "refuse_takeover",
    ),
    Case(
        "eval-e10",
        "error",
        "Choose migrate_implicitly or reject_version.",
        (
            "Stored checkpoint schema is v99; installed runtime understands only v1 and v2.",
            "No explicit migration exists. Unknown future schemas must fail closed.",
        ),
        "reject_version",
    ),
)

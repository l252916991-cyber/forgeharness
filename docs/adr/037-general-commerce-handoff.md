# ADR 037: Product-independent commerce handoff

Date: 2026-10-27. Status: accepted for the productization working tree; candidate qualification pending.

## Context

The consultation application could create an approved support ticket only after a
product had been identified. A customer who explicitly requested a person while no
catalogue item matched received `needs_human/no_evidence`, but the workflow could
not create the handoff it had just recommended. Inventing a product identity would
violate the evidence boundary, while writing a ticket directly from the HTTP route
would bypass the Harness approval and invocation journal.

## Decision

- Register `create_general_handoff_ticket` in the consultation application. It is
  an idempotent write governed by the same independent supervisor approval policy,
  durable journal and receipt verification as product-bound handoffs.
- Store a general ticket in the existing tenant-scoped ledger with an empty `sku`.
  Product inputs require a non-empty SKU, so the marker is unambiguous. Do not
  invent a product version or source anchor when none exists.
- Bind business deduplication to `(tenant, request_id)` and a digest of the general
  handoff reason. Exact retries return the existing ticket; a changed payload is a
  conflict. Keep the physical invocation key check as a separate boundary.
- Create a general ticket only when the user explicitly requests handoff. A plain
  no-evidence query remains a successful `needs_human/no_evidence` response so the
  user can refine the request without causing a write.
- Let authorized support and supervisor roles read general tickets within their
  tenant even though no product resource can be present. Product-bound ticket reads
  retain their product-scope check.
- Advance the `commerce-consultation` application and policy versions from 1 to 2.
  Existing v1 sessions are not silently adopted; they remain bound to their original
  application definition.

## Consequences

The synthetic console now has a complete no-product escalation path while retaining
exact approval, deduplication and receipt guarantees. This does not supply real
merchant identity, a real ticketing API, natural-language clarification quality or
business qualification. Those remain explicit productization dependencies.

Regression coverage must include two sessions using the same business request,
payload conflict, approval before effect, and support reads with no product scope.
Any later external ticket adapter must preserve these semantics and return a
queryable business receipt.

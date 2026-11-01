# ADR 036: Bind model invocation tickets to storage and complete run identity

Date: 2026-10-27. Status: accepted; supersedes the invocation-key detail in ADR 035.

## Confirmed failure

The 0.9.0 candidate used the public run namespace and node ID as a provider
capacity key. Separate private state directories can contain the same public
context. A completed node in the first directory could then appear to prove that
an unrelated unresolved claim from the second directory had ended. Resuming the
first run could release the second run's physical capacity without a receipt.
This was reproduced after candidate-r2's gates passed (PE-012); that candidate
must not qualify the corrected implementation.

## Decision

- Mint a SHA-256 model invocation identity over protocol tag, canonical state
  database path, complete persisted run identity and node ID. Persist it before
  queueing/sending and reuse that exact value during resume. Owner turnover does
  not mint a new model invocation; already-sent unknown calls still never retry.
- Give model reconciliation the persisted invocation ID as its lookup key. Tool
  journal/idempotency keys retain their existing contract. A missing model sending
  identity is a refusal, not permission to invent a past invocation.
- Register model receipts under the supplied invocation ID. A node's checkpoint
  can release only the claim with that same scoped identity. Inputs can retain
  equal public task IDs across isolated benchmark arms without sharing capacity.
- Release as SDK 0.9.1. State serialization stays schema 3 / recovery 5; the
  complete identity includes the framework version and rejects 0.9.0 checkpoints.
  Retain old wheels and evidence; no implicit adoption of earlier candidates.

## Verification

Use identical public context, task, graph and model configuration in two private
stores sharing one dispatcher. Complete one branch in the first, leave another
unknown, then leave the corresponding branch unknown in the second. Resuming the
first must not release the second's claim. This test fails on 0.9.0 and passes on
the scoped implementation. Also verify the resolver receives the persisted key,
resuming retains it, and old-version identity cannot be taken over.

Freeze candidate-r3 and run every gate again. Prior 104/104 candidate-r2 evidence
remains historical; it does not override a subsequently confirmed boundary defect.

# ADR 011 — Durable approvals and safe local recovery (accepted)

SQLite is the first transactional state configuration. A consumed approval and
checkpoint transition from awaiting approval to an admitted RUNNING call commit
in one transaction in the same database. Persisted authorization names the exact
logical call; a crash before journal creation does not lose the execution right.
Missing authorization does not allow a fresh non-idempotent side effect.

Grants bind exact call ID/name/arguments and the complete trusted run binding.
Approval is one-shot, expires, and is rechecked against current policy. A grant
cannot authorize altered scope/version/identity. In-memory/legacy ledgers retain
weaker non-durable behavior and are not covered by this transaction guarantee.

Recovery claims the checkpoint revision before proceeding. Completed journal
outputs must match their digest and call identity. Claimed calls can start;
started read-only/idempotent calls may repeat under the same key, physical attempt
budget and current authorization. Unknown non-idempotent effects stop. SQLite CAS
arbitrates competing recovery snapshots. Recovery requires the previous worker to
be stopped; there is no distributed lease/fencing or external exactly-once claim.
Business adapters need their own unique request key and transaction/ledger.

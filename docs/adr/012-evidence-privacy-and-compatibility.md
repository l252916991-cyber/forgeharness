# ADR 012 — Evidence/privacy and compatibility (accepted)

Public SDK API=1; checkpoint/event/receipt contracts=1; recovery generation=3.
The distribution is 0.2.0. Generation 2 historical model/evaluation evidence is
retained and is not relabelled as evidence for this candidate.

Every SDK model-bound input and persisted observation passes the same deterministic
host EvidenceTransform. Common credential patterns are masked by default; domain
PII policy is host supplied. Actual transformed requests, not input hashes alone,
are recorded. Default hash-chain redaction is applied before the SDK boundary too,
so ordinary credential masking cannot cause request/trace divergence. Heuristics
are not a secret-management or universal PII detection claim.

Local SQLite/JSONL evidence is scoped, held in a private state directory and can be
logically purged with a permanent run tombstone. Erasure is retryable after a crash
between DB deletion and file unlink. secure_delete/WAL truncation/VACUUM reduce local
residual data; OS backups, SSD physical erasure and business/attachment ledgers are
owned by the host. Tombstones retain only hashed identifiers/bindings.

Hash-chain canonical v1 data excludes the default event-version field, preserving
existing trace verification. Validation accepts version 1 only. No final external
anchor exists: truncation/whole replacement detection is not promised.

Optional extras keep core installation independent of web, PDF, vector/queue/model
runtimes. The sdist explicitly allowlists source, license, docs and examples;
local archives, data, reports and credentials are never implicit build inputs.
Historical CLI modules remain exactly aliased when cli dependencies exist;
minimal installation offers help and a clear extra-install message. Compatibility
imports of coding verifiers/maps are lazy and never loaded on ordinary core import.

SDK local file locks cover active execution and evidence writes, alongside database
CAS. The supported host is POSIX single-machine local storage, not Windows or
network filesystem/distributed execution. Legacy adapters have their documented
weaker guarantees and are outside the new multi-identity SDK certification.

# Enhanced-candidate security review — 2026-09-04

## Scope and decision

Reviewed the local FastAPI/vanilla-JavaScript, document ingestion, RAG evidence,
memory, and CodingAgent boundaries using the security-best-practices skill.
This is a single-user localhost development profile, not an authenticated service.
The full release remains **on hold** for the scope/evidence gaps in `AUDIT.md`.

## Closed findings

### FH-009 — High: browser access to the unauthenticated local API

- Rules: FASTAPI-HOST-001, FASTAPI-HEADERS-001, JS-CSP-002, JS-XSS-001, FS-DOMC-001.
- Location: `src/forgeharness/api.py:173`, `api.py:179`, `api.py:195` and `_UI_SCRIPT`.
- Evidence: loopback binding alone does not reject a DNS-rebinding Host, and browser-origin mutations require a separate guard. Model/file output is untrusted browser content.
- Impact: a hostile browser page could target local state if these boundaries were absent.
- Fix: explicit localhost Host allowlist; same-origin mutation check when Origin is present; hash-based script CSP; textContent rendering; explicit element lookup and event listeners. Nginx preserves the original Host port.
- Validation: `test_local_api_host_origin_and_browser_boundary`; Edge upload/chat smoke test. The interface has no raw HTML rendering or third-party script dependency.
- Limit: CLI requests without Origin remain permitted; this is not user authentication or malware protection. Do not publish the port or tunnel it.

### FH-010 — High: revoked memory could enter the knowledge-answer path

- Rules: project review-gated-memory invariant; analogous to FASTAPI-AUTHZ-001 authoritative object-state enforcement.
- Location: `knowledge/application.py:199`, `knowledge/memory.py:63`, `knowledge/memory.py:91`.
- Evidence: the earlier composition supplied raw `HybridRetriever` to KnowledgeService instead of SemanticMemoryService. A failed vector deletion left retrievable payloads even though the dedicated memory search filtered them.
- Impact: an answer could re-expose revoked memory after remote cleanup failure.
- Fix: both paths use the same approved-and-indexed record filter; revocation persists before index removal, and repeated cleanup is safe.
- Validation: `test_memory_revocation_suppresses_stale_vectors_when_cleanup_fails` now also verifies knowledge search and the final answer refuse the stale memory.
- Limit: filtering after retrieval does not securely erase audit records or old backups. A future external reranker would need pre-model filtering; the current deployment uses local OMLX.

### FH-011 — High: media identifiers were not resolved to selected evidence

- Rules: FASTAPI-VALID-001, FASTAPI-AUTHZ-001 within the single-owner data profile.
- Location: `knowledge/conversation.py::send`, `knowledge/service.py::attachment/search`.
- Evidence: adding document hashes to a text query did not prove the answer used the selected file; valid-shaped nonexistent IDs were accepted.
- Fix: verify persisted/indexed document identity before saving the message, preserve image/file ContentPart, and build bounded attachment evidence exclusively from selected documents.
- Validation: `test_unknown_unindexed_and_image_attachments`, `test_api_attachments_are_resolved_and_knowledge_runs_survive_restart`.
- Limit: first-five-chunk attachment excerpts are not full-document analysis; there are no tenant ACLs in this profile.

## Accepted limited-profile risks

- FASTAPI-AUTH-001 / AUTHZ-001: there is no identity or tenant authorization. Sessions separate histories, not mutually distrusting users. Any public deployment requires a separate authentication/authorization design.
- FASTAPI-SUPPLY-001: the locked runtime/dev/platform/benchmark audit has no known vulnerability at audit time. Optional LlamaIndex pulls one NLTK advisory in the all-extras report; comparison extras are excluded from normal setup/CI. Do not use affected model-artifact loading APIs.
- Workspace checks and exact approval are not an OS sandbox; run untrusted code only in a disposable isolated environment. A Git directory does not prove safe repository contents.
- Trace hashing detects modifications relative to an existing chain; it is not a signature, encryption, or protection against an attacker rewriting the entire chain. Credential-pattern redaction is not comprehensive private-source classification.
- Ordinary exception messages, memory audit content, and model-derived image descriptions stay local and must not be committed as private evidence. The checked-in reports use synthetic/public fixtures.

## Dependency/behavior interpretation

No claim is made that security headers eliminate prompt injection. Evidence remains
untrusted model input; deterministic tool permissions prevent a generated answer
from authorizing a write. Citation existence is not semantic truth. The Reviewer
is read-only and default-off until representative quality evidence warrants it.

# ADR 016: tool-read source dependencies and withdrawal

Date: 2026-10-14. Status: implemented in the 0.5.0 synthetic candidate.

## Need

A conversation turn can obtain governed material through a **tool** (a published
record read, a case search, a product lookup) and not only through a registered
context provider. Only provider material was persisted on the turn, so once a
source was withdrawn, superseded or expired, the stored tool output was still
served by `history` and `inspect` and was still offered to the model as history
(`reports/SESSION_CURRENT_STATE_REVIEW.md` STATE-001, reproduced). Context material
validation was already mandatory; tool-read sources had no equivalent contract, and
a documentation reminder or an always-true validator is not a guarantee.

## Decision

1. A tool declares that its results depend on governed sources through
   `ToolSpec.governed_sources: bool = False`. The compatibility default leaves existing tool contracts unchanged. A host must
   explicitly declare source-reading tools; undeclared dependencies are not automatically governed.
2. A result whose `BusinessReceipt` carries `resource_id`, `version` and
   `evidence_refs` yields `SourceRef` entries (`resource_id`, `version`, `source`,
   `locator`), parsed from the documented `source@version#locator` anchor form. Only
   tools that declared `governed_sources` contribute; other receipts stay execution
   evidence, not source dependencies.
3. `SessionTurn.sources` persists those references, bounded and transformed exactly
   like the rest of the turn payload. Persisting a reference grants no trust in its
   content.
4. `SessionService` requires a host `validate_sources(context, refs) -> bool`
   callback whenever a registered tool declares `governed_sources`, mirroring the
   existing rule that context providers require `validate_materials`. The callback
   queries the host's current ledger; the framework never assumes a version is
   still current or still visible to the caller.
5. Revalidation happens before a stored turn is returned, before history is
   assembled for a new turn, before `inspect` and `events`, and before approval or
   resume. A reference that is no longer current makes the turn unavailable instead
   of being silently filtered or rewritten: an answer whose sources changed must not
   stay resumable.
6. Turn and session deletion keep covering these references. They hold identifiers,
   never copied content, so deletion of the source stays with its owner.
7. The change is additive: applications whose tools do not declare governed sources
   keep the previous behaviour, and SDK API v1 is preserved.

## Consequences and alternatives

* The host must implement a second validator. A missing validator fails closed at
  construction time, so a host cannot accidentally run without one; the cost is
  explicit configuration, not an optional check.
* Rejected: treating every receipt as a governed source. That would force unrelated
  tools (formatters, calculators) to declare validators and would overload
  `evidence_refs` with two different meanings.
* Rejected: revalidating by comparing a stored copy of the quoted text. That proves
  the text did not change, not that the caller may still read it.
* Residual: references point at identifiers the host declared. The framework cannot
  detect a source the host never declared, and host-side backups, external
  attachments and domain retention remain outside its reach.

## Verification

`tests/unit/test_sessions.py` covers withdrawal after a tool read blocking history,
`inspect`, the next turn's model input, approval and resume; `tests/unit/test_evidence.py`
covers the published-record tool declaring governed sources; S2 retrieval adds the
index-hit case, where a withdrawn record must stop appearing as a candidate.

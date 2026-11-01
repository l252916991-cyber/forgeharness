# ADR 045: A skill install loop that proves the package before trusting it

Date: 2026-10-30. Status: implemented.

## Context

`skills/loader.py` was strict and complete: manifest validation, tool whitelist,
symlink refusal, duplicate-name refusal. What it could not answer is the question a
*downloaded* package raises: does this thing work here?

A skill is instructions that change what every later run does. Admitting one without
evidence means a permanently wrong system prompt that no test covers, installed by a
single tool call.

## Decision

`skills/install.py` runs five stages, and each one exists because skipping it has a named
failure:

1. **stage** -- fetch into a scratch directory, bounded in bytes and restricted by scheme
   and host allowlist. Writing straight into the skills root would let a half-downloaded
   package be loaded by the next run.
2. **validate** -- the strict loader parses it. A manifest that does not round-trip is not
   a skill, it is text that looks like one.
3. **trim** -- capabilities this deployment does not compose are removed and *recorded*
   (`dropped_tools`, traced as `skill.trimmed`). Rejecting instead would force every
   deployment to maintain a fork of the same package; doing it silently would hide that a
   package lost half its tools.
4. **smoke** -- a declared `smoke_command` requires an explicitly configured backend.
   There is no default host runner. Operators handling untrusted packages must configure
   their container boundary; choosing `LocalBackend` explicitly runs trusted code on the
   host and is not a sandbox. A non-zero exit fails the install. A package with no smoke
   command fails too when `require_smoke` is on (default).
5. **promote** -- write the *trimmed* manifest and serialize same-name writers with a
   nonblocking POSIX file lock. Directory and manifest operations use a no-follow open
   directory chain, including the lock file, to reject symlink destinations. Identical
   bytes are a no-op; different bytes are refused unless `allow_upgrade`. An upgrade
   renames the complete old package to a random hidden backup, promotes the new package,
   and removes the backup after success. A failed promotion restores the old directory.
   Hidden staging and backup directories are never loaded as active skills.

Frontmatter has deterministic field ordering and JSON string escaping, so literal
newlines and quotes round-trip without depending on PyYAML emitter behavior. Local and
remote fetches read at most the policy byte limit plus one byte. Every remote redirect
rechecks HTTP(S), HTTPS requirements, host allowlists and credential-free authority before
opening the destination, with an explicit total hop limit. Trace source labels omit URL
queries and fragments; a full-source SHA-256 retains identity, and all trace payloads
pass through mandatory credential masking.

Failures carry a `code` (`source_rejected`, `too_large`, `fetch_failed`, `invalid_package`,
`smoke_missing`, `smoke_backend_required`, `smoke_failed`, `already_installed`,
`destination_rejected`, `install_conflict`) so a caller can distinguish "this
package is bad" from "this deployment is offline" without catching `Exception`.

Assignment to a sub-agent is a separate field, not a rewrite: `SubAgentSpec.skill_instructions`
is appended *after* the operator's brief, because a skill is reusable advice and the
operator's scope and limits are what make it applicable here. Putting the package first
would let a downloaded skill outrank the delegation boundary.

## Compaction spill: the same problem in the other direction

`context/compression.py` already spilled tool observations. Message-history compaction
did not: dropped turns were truncated to a preview and the originals existed nowhere.

`context/spill.py` generalizes the store (`BlobStore` with an `obs://` or `ctx://` prefix,
content-addressed, sha256-verified on read), and `ContextAssembler` writes the exact
dropped messages before the summary replaces them, returning `compaction_ref`. The
summary header carries `full: ctx://...` -- and under a tight budget the reference is
kept *first* in the shortened header, because a pointer nobody is told about is a file
nobody can use. `read_compacted_context` pages the originals back with `offset`/`limit`.

Pinning the objective (ADR 042) removes half of this problem by construction: the one
message whose absence is unrecoverable no longer enters the dropped set.

## Consequences

The install path needs a backend and may need a container image; both are injected, so the
pipeline is testable without Docker and a deployment can point it at the same sandbox it
uses for commands.

An upgrade spans two directory renames. A process or machine crash between them can
leave the old package in its hidden backup and no active package. This is an accepted
availability limitation, not crash-atomic replacement: the operator must inspect and
restore the retained backup before retrying. An error restoring a backup also preserves
it for operator recovery. Installer locks protect cooperating installers; the destination
must remain an operator-controlled directory. Full container execution is qualified
separately from these installer unit tests.

Skills remain optional. Importing the base SDK or hosted contracts does not import
PyYAML; requesting the Skills public API loads its `skills` extra lazily.

The installer now requires an explicitly configured backend before running package
smoke commands. Choosing a host backend is a trusted operator decision; trimming
declared tools does not sandbox a smoke command. Every redirect is revalidated
against the source scheme/host policy and bounded by a hop limit. Trace sources
exclude URL query/fragment secrets and retain a digest for exact source identity.
Optional Skill imports stay lazy so the base SDK and Cloud do not require PyYAML.

Promotion serializes package writers and preserves the prior package when an
ordinary rename fails. Upgrade uses two renames: a hard crash between them may
leave a hidden backup for operator recovery. It is not a cross-crash atomic
upgrade, and installation must be drained/reconciled before production upgrade.

Spill storage opens every POSIX directory component without following symlinks,
uses exclusive random temporary files and descriptor-relative atomic promotion,
refuses symlink/hardlink/nonregular blob reads, verifies existing bytes, and caps
one blob at 16MiB. Unsafe paths fail closed, including symlink ancestors; a trusted
host should bind a real workspace path. This is file-access confinement, not a
sandbox for an arbitrary host process running as the same user.

`LoadedSkill.trimmed()` is additive: `validate_capabilities()` still exists for callers
that want to refuse rather than reduce.

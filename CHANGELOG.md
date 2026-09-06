# Changelog

All notable changes to ForgeHarness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/). The authoritative implemented/planned boundary
remains `docs/STATUS.md`.

## [Unreleased]

### Changed

- CI now runs `make gate` instead of `make check`: the release gate adds the
  keyless control evaluation, a CLI contract smoke test, lockfile consistency
  (`uv lock --check`), and a `pip-audit` vulnerability scan (PYSEC-2026-3740
  ignored as the documented accepted residual risk, see
  `docs/review/FINDINGS.json` FH-008).
- Added a structured JSON logging layer (`forgeharness.observability.logging`)
  writing one JSON object per line to stderr; the HTTP API logs every request
  with method/path/status/duration and blocks cross-origin mutations as
  warning-level events. Complements, does not replace, the hash-chained trace.

### Fixed

- Framework-comparison CLI commands (`forge langchain-rag`, `forge
  langchain-agent`, `forge framework-compare`) are now actually registered on
  the Typer application; the README previously documented commands that did not
  exist. A CLI contract test (`tests/unit/test_cli_contract.py`) prevents
  documentation from outrunning the CLI surface again.
- The LangChain RAG arm ran its retrieval stage twice per query and crashed on
  Pydantic field assignment (`HybridRetriever`); both defects are fixed and
  covered by unit tests.
- `pyproject.toml` declared `langchain>=0.3,<1` alongside
  `langchain-core>=1,<2`, which is unsatisfiable; the frameworks extra now pins
  the mutually compatible 1.x generation (langchain, langchain-core,
  langchain-openai, langgraph) and drops the unused `langchain-community`.
- The LangChain chat client now bypasses desktop proxies (`trust_env=False`),
  matching the native OMLX adapter, so loopback model calls no longer fail
  through a system proxy.

### Added

- `forge framework-compare` writes a revision-bound measured benchmark
  (`reports/framework-comparison.json`) of native retrieval versus the LangChain
  path over identical queries; missing optional dependencies are recorded as a
  skipped arm instead of estimated numbers.

### Removed

- Process/status summary documents duplicated across the repository root and
  `docs/` were consolidated; superseded material now lives under
  `docs/archive/`. `docs/STATUS.md` remains the single source of truth for the
  implemented/planned boundary.

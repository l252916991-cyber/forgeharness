.PHONY: sync format lint typecheck test check gate review

sync:
	uv sync --extra dev --extra platform --extra benchmark

format:
	uv run --no-sync ruff format .
	uv run --no-sync ruff check --fix .

lint:
	uv run --no-sync ruff format --check .
	uv run --no-sync ruff check .

typecheck:
	uv run --no-sync mypy

test:
	uv run --no-sync pytest --cov=forgeharness --cov-report=term-missing --cov-report=json:reports/coverage.json
	uv run --no-sync python benchmarks/check_branch_coverage.py

check: lint typecheck test

# Release gates beyond lint/type/test: keyless control evaluation, CLI contract
# smoke, lockfile consistency, and a dependency vulnerability audit. The NLTK
# advisory (PYSEC-2026-3740) is an accepted residual risk reachable only through
# the optional LlamaIndex comparison extra; see docs/review/FINDINGS.json FH-008.
gate: check
	uv run --no-sync forge eval-control
	uv run --no-sync forge --help
	uv run --no-sync forge framework-compare --help
	uv lock --check
	uv export --frozen --no-emit-project --format requirements-txt -o /tmp/forgeharness-audit-requirements.txt
	uv run --no-sync --with pip-audit pip-audit -r /tmp/forgeharness-audit-requirements.txt --ignore-vuln PYSEC-2026-3740

review: check
	uv run --no-sync python -m forgeharness.review

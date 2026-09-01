.PHONY: sync format lint typecheck test check review

sync:
	uv sync --all-extras

format:
	uv run --no-sync ruff format .
	uv run --no-sync ruff check --fix .

lint:
	uv run --no-sync ruff format --check .
	uv run --no-sync ruff check .

typecheck:
	uv run --no-sync mypy

test:
	uv run --no-sync pytest --cov=forgeharness --cov-report=term-missing

check: lint typecheck test

review: check
	uv run --no-sync python -m forgeharness.review

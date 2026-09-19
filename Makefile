.PHONY: help setup db-up db-down fmt lint test fixtures

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | sed 's/^/  make /'

setup:          ## install python + node deps
	uv sync
	pnpm install

db-up:          ## start postgres
	docker compose up -d postgres

db-down:
	docker compose down

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

test:
	uv run pytest

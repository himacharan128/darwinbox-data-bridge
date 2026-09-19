.PHONY: help setup web db-up db-down fmt lint test eval run demo

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | sed 's/^/  make /'

setup:          ## install python and node dependencies
	uv sync
	pnpm install

web:            ## build the UI bundle the API serves
	pnpm --filter @dbx/web build

run: web        ## start the destination, the API, and the console
	./scripts/dev.sh

test:
	uv run pytest -q

lint:
	uv run ruff check packages services scripts tests

fmt:
	uv run ruff format packages services scripts tests

eval:           ## measure the escalation boundary against the labelled corpus
	uv run python scripts/eval_mapping.py

sweep:          ## show how the boundary moves with the thresholds
	uv run python scripts/eval_mapping.py --sweep

demo:           ## run one migration in the terminal
	uv run python scripts/run_migration.py

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

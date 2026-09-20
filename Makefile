.PHONY: help setup web db-up db-down fmt lint test eval sweep run demo samples e2e deploy teardown stack stack-down

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

samples:        ## run every sample set and report what each produced
	uv run python scripts/run_samples.py

demo:           ## run one migration in the terminal
	uv run python scripts/run_migration.py

deploy:         ## build, push and roll out to ECS
	./scripts/deploy.sh

teardown:       ## remove every AWS resource this project created
	./scripts/teardown.sh

e2e:            ## end-to-end checks against a deployment (URL=... to target one)
	uv run python scripts/e2e_hosted.py $${URL:-http://localhost:8080}

stack:          ## build and run the whole stack in containers
	docker compose up --build

stack-down:
	docker compose down -v

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

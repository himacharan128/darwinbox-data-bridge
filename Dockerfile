# One image, two entry points. The API and the mock destination share all their
# Python dependencies, so building them separately would double the layer cache for
# no benefit; the compose file picks which process to run.

FROM node:22-alpine AS web
WORKDIR /build
COPY package.json pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN corepack enable && pnpm install --frozen-lockfile=false
COPY apps/web apps/web
RUN pnpm --filter @dbx/web build

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Dependency layer first: source changes should not reinstall the world.
COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/migration-core/pyproject.toml packages/migration-core/
COPY packages/extraction/pyproject.toml packages/extraction/
COPY packages/agent/pyproject.toml packages/agent/
COPY services/api/pyproject.toml services/api/
COPY services/worker/pyproject.toml services/worker/
COPY services/mock-target/pyproject.toml services/mock-target/
RUN uv sync --frozen --no-install-workspace --no-dev

COPY packages packages
COPY services services
COPY scripts scripts
COPY tests/fixtures tests/fixtures
COPY --from=web /build/apps/web/dist apps/web/dist
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8080
CMD ["uvicorn", "dbx_api:app", "--host", "0.0.0.0", "--port", "8080"]

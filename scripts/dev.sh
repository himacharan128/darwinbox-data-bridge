#!/usr/bin/env bash
# Start the mock destination and the migration API, and serve the built UI.
set -euo pipefail
cd "$(dirname "$0")/.."

export MOCK_TARGET_URL="${MOCK_TARGET_URL:-http://127.0.0.1:8081}"
export MOCK_TARGET_DB="${MOCK_TARGET_DB:-.artifacts/mock-target.db}"
export DBX_DATA_DIR="${DBX_DATA_DIR:-.artifacts}"

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

uv run uvicorn dbx_mock_target:app --host 127.0.0.1 --port 8081 --log-level warning &
sleep 1
uv run uvicorn dbx_api:app --host 127.0.0.1 --port 8080 --log-level warning &

echo
echo "  Migration console : http://127.0.0.1:8080"
echo "  Mock destination  : http://127.0.0.1:8081/docs"
echo "  Ctrl-C to stop."
echo
wait

#!/bin/sh
set -eu

export FORGE_ENABLE_OMLX=true
export FORGE_OMLX_BASE_URL=http://127.0.0.1:8000/v1
export FORGE_POSTGRES_DSN=postgresql://forge:forge-local-only@127.0.0.1:5432/forgeharness
export FORGE_REDIS_URL=redis://127.0.0.1:6379/0
export FORGE_QDRANT_URL=http://127.0.0.1:6333

echo "Start these in separate terminals:"
echo "uv run forge serve --port 8001"
echo "uv run forge serve --port 8002"
echo "uv run arq forgeharness.worker.WorkerSettings"

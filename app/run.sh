#!/bin/bash
# Start the OSM console.
cd "$(dirname "$0")"
export OSM_HOME="${OSM_HOME:-/opt/ocpstorage}"
HOST=${OSM_HOST:-0.0.0.0}
PORT=${OSM_PORT:-8800}
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"

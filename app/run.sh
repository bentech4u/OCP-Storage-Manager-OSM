#!/bin/bash
# Start the OSM console. Set OSM_TLS_CERT and OSM_TLS_KEY to serve https, which the
# Microsoft sign-in page requires for its redirect address.
cd "$(dirname "$0")"
export OSM_HOME="${OSM_HOME:-/opt/ocpstorage}"
HOST=${OSM_HOST:-0.0.0.0}
PORT=${OSM_PORT:-8800}
TLS=()
if [ -n "${OSM_TLS_CERT:-}" ] && [ -n "${OSM_TLS_KEY:-}" ]; then
  TLS=(--ssl-certfile "$OSM_TLS_CERT" --ssl-keyfile "$OSM_TLS_KEY")
fi
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" "${TLS[@]}" "$@"

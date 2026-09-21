#!/bin/bash
# Start the OSM console.
#   OSM_PORT / OSM_HOST      where to listen (0.0.0.0:8800 by default)
#   OSM_TLS_CERT / _KEY      serve https directly
#   OSM_TRUSTED_PROXIES      addresses whose forwarded headers are believed, for a
#                            reverse proxy or a Cloudflare tunnel on this host
cd "$(dirname "$0")"
export OSM_HOME="${OSM_HOME:-/opt/ocpstorage}"
HOST=${OSM_HOST:-0.0.0.0}
PORT=${OSM_PORT:-8800}
ARGS=(--host "$HOST" --port "$PORT")
if [ -n "${OSM_TLS_CERT:-}" ] && [ -n "${OSM_TLS_KEY:-}" ]; then
  ARGS+=(--ssl-certfile "$OSM_TLS_CERT" --ssl-keyfile "$OSM_TLS_KEY")
fi
ARGS+=(--proxy-headers --forwarded-allow-ips "${OSM_TRUSTED_PROXIES:-127.0.0.1}")
exec .venv/bin/uvicorn app.main:app "${ARGS[@]}" "$@"

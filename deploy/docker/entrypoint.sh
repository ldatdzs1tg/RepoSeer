#!/usr/bin/env sh
set -eu

PORT="${PORT:-10000}"
mkdir -p /tmp/reposeer-data

# Render nginx config with runtime $PORT
sed "s/__PORT__/${PORT}/g" /app/deploy/docker/nginx.conf.template > /tmp/nginx.conf

# Start backend demo API on loopback (127.0.0.1:8765)
uv run --frozen --no-dev reposeer demo-api \
  --port 8765 \
  --data-dir /tmp/reposeer-data \
  --input-bundle /app/examples/application/synthetic_bundle.json \
  --model /app/examples/application/synthetic_model_a.json \
  --agent-replay /app/examples/application/synthetic_agent_replay.json \
  --mock-dir /app/examples/analysis &

API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT INT TERM

# Wait briefly until backend API is ready
i=0
while [ "$i" -lt 40 ]; do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=1)" >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 0.25
done

# Serve frontend + reverse-proxy /api/* on $PORT
exec nginx -c /tmp/nginx.conf -g "daemon off;"

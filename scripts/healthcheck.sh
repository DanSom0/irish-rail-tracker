#!/usr/bin/env bash
set -euo pipefail

url=${1:?Usage: healthcheck.sh <health-url>}
deadline=$((SECONDS + 60))
echo "[health] Waiting up to 60 seconds for HTTP 200"
while (( SECONDS < deadline )); do
  status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 3 "$url" || true)
  if [[ "$status" == 200 ]]; then
    echo "[health] HTTP 200: healthy"
    exit 0
  fi
  echo "[health] HTTP ${status:-unknown}; retrying"
  sleep 2
done
echo "[health] Failed: endpoint did not return HTTP 200" >&2
exit 1

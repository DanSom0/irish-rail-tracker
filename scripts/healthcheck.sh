#!/usr/bin/env bash
set -euo pipefail

# Usage: healthcheck.sh [--release <sha>] <health-url>
# Waits up to 60 seconds for HTTP 200. With --release, the JSON body must also report that
# release, so a healthy older release does not pass for the one just deployed.
release=""
if [[ ${1:-} == --release ]]; then
  release=${2:?Usage: healthcheck.sh [--release <sha>] <health-url>}
  shift 2
fi
url=${1:?Usage: healthcheck.sh [--release <sha>] <health-url>}
body=$(mktemp)
trap 'rm -f "$body"' EXIT
deadline=$((SECONDS + 60))
echo "[health] Waiting up to 60 seconds for HTTP 200${release:+ from release $release}"
while (( SECONDS < deadline )); do
  status=$(curl --silent --show-error --output "$body" --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 3 "$url" || true)
  if [[ "$status" == 200 && -z $release ]]; then
    echo "[health] HTTP 200: healthy"
    exit 0
  fi
  if [[ "$status" == 200 ]]; then
    running=$(grep -Eo '"release": ?("[^"]*"|null)' "$body" | sed -E 's/.*: ?"?([^"]*)"?/\1/' || true)
    if [[ $running == "$release" ]]; then
      echo "[health] HTTP 200: healthy; release $release"
      exit 0
    fi
    echo "[health] HTTP 200 but release is ${running:-not reported}; retrying"
  else
    echo "[health] HTTP ${status:-unknown}; retrying"
  fi
  sleep 2
done
if [[ -n $release ]]; then
  echo "[health] Failed: endpoint did not return HTTP 200 from release $release" >&2
else
  echo "[health] Failed: endpoint did not return HTTP 200" >&2
fi
exit 1

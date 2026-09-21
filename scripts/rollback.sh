#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 || ! $1 =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: rollback.sh <full-40-character-commit-sha>" >&2
  exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export IMAGE_TAG=$1
trap 'echo "[rollback] Failed; inspect docker compose logs" >&2' ERR

echo "[rollback] Deploying $IMAGE_TAG"
# Tagged images survive deploy's prune, so a cached rollback needs no GHCR token.
if ! docker image inspect "ghcr.io/dansom0/irish-rail-tracker:$IMAGE_TAG" > /dev/null 2>&1; then
  docker compose --env-file .env -f docker-compose.prod.yml pull web worker
fi
docker compose --env-file .env -f docker-compose.prod.yml up -d
./scripts/healthcheck.sh http://localhost/health
echo "[rollback] Healthy at $IMAGE_TAG"

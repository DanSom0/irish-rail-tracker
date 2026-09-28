#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy runs this from the new release's bundle before activate-release.sh. It pulls the
# release image if the server does not have it and runs `alembic upgrade head` in a one-off
# container from it, so the schema is migrated before the new web and worker start. If it
# fails, the deploy stops and the running release is untouched. Rollback never runs it: an
# older image cannot know a newer revision, so every migration must keep working with the
# release before it.
if [[ $# != 1 || ! $1 =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: migrate.sh <full-40-character-commit-sha>" >&2
  exit 1
fi
target=$1
IMAGE=ghcr.io/dansom0/irish-rail-tracker
# shellcheck source=scripts/app-dir.sh
source "$(dirname "${BASH_SOURCE[0]}")/app-dir.sh"
trap 'echo "[migrate] Failed; the running release was not changed" >&2' ERR

# Deploy feeds its remote script to `bash -s` on stdin; Compose must not read the rest of it.
compose() {
  docker compose --env-file .env -f "releases/$target/docker-compose.prod.yml" "$@" < /dev/null
}
alembic() {
  compose run --rm --no-deps -T web alembic "$@"
}

if [[ ! -f releases/$target/docker-compose.prod.yml ]]; then
  echo "[migrate] No release bundle in $PWD/releases/$target; see docs/operations.md" >&2
  exit 1
fi

export IMAGE_TAG=$target
if ! docker image inspect "$IMAGE:$target" > /dev/null 2>&1; then
  echo "[migrate] Pulling $target"
  compose pull web worker
fi
# Starts the database on a new server; a running, unchanged database is left as it is.
compose up -d --wait db
before=$(alembic current)
echo "[migrate] Revision before: ${before:-none}"
# The baseline refuses to run over tables that were never stamped (scripts/stamp-baseline.sh).
alembic upgrade head
after=$(alembic current)
echo "[migrate] Revision after: $after"

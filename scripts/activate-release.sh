#!/usr/bin/env bash
set -euo pipefail

# Start the release in releases/<sha> (Compose file and scripts) with its image. Only after
# /health passes do .env, `current` and the top-level links move to it. If the check fails,
# the previous release is started again and stays current.
IMAGE=ghcr.io/dansom0/irish-rail-tracker

pull=false
if [[ ${1:-} == --pull ]]; then
  pull=true
  shift
fi
if [[ $# != 1 || ! $1 =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: activate-release.sh [--pull] <full-40-character-commit-sha>" >&2
  exit 1
fi
target=$1
# shellcheck source=scripts/app-dir.sh
source "$(dirname "${BASH_SOURCE[0]}")/app-dir.sh"
trap 'echo "[release] Failed; inspect docker compose logs" >&2' ERR

compose() {
  local release=$1
  shift
  docker compose --env-file .env -f "releases/$release/docker-compose.prod.yml" "$@"
}

if [[ ! -f releases/$target/docker-compose.prod.yml || ! -d releases/$target/scripts ]]; then
  echo "[release] No release bundle in $PWD/releases/$target; see docs/operations.md" >&2
  exit 1
fi

# Servers deployed before release bundles keep the running release's files at the top
# level. Keep those files as that release's bundle so it can be restarted or rolled back to.
running=$(sed -n 's/^IMAGE_TAG=//p' .env | tail -n 1)
if [[ ! -L current && -f docker-compose.prod.yml && ! -L docker-compose.prod.yml \
      && $running =~ ^[0-9a-f]{40}$ ]]; then
  if [[ ! -e releases/$running ]]; then
    rm -rf "releases/.adopt-$running"
    mkdir "releases/.adopt-$running"
    cp -R docker-compose.prod.yml scripts "releases/.adopt-$running/"
    mv "releases/.adopt-$running" "releases/$running"
    echo "[release] Kept the running files as releases/$running"
  fi
  ln -sfn "releases/$running" current
fi
previous=""
if [[ -L current ]]; then
  previous=$(basename "$(readlink current)")
fi

export IMAGE_TAG=$target
echo "[release] Starting $target (previous: ${previous:-none})"
if $pull; then
  compose "$target" pull
elif ! docker image inspect "$IMAGE:$target" > /dev/null 2>&1; then
  compose "$target" pull web worker
fi
# --remove-orphans stops services that the other release's Compose file defines.
if ! compose "$target" up -d --remove-orphans \
    || ! "$SCRIPT_DIR/healthcheck.sh" http://localhost/health; then
  if [[ -z $previous || $previous == "$target" ]]; then
    echo "[release] $target is unhealthy and there is no previous release to restart" >&2
    exit 1
  fi
  echo "[release] $target is unhealthy; restarting $previous" >&2
  IMAGE_TAG=$previous compose "$previous" up -d --remove-orphans
  "$SCRIPT_DIR/healthcheck.sh" http://localhost/health
  echo "[release] Failed: $previous is running and still current" >&2
  exit 1
fi

"$SCRIPT_DIR/pin-image-tag.sh" "$target"
ln -sfn "releases/$target" current
for path in docker-compose.prod.yml scripts; do
  if [[ ! -L $path ]]; then
    rm -rf "$path"  # Files from before release bundles, adopted above.
  fi
  ln -sfn "current/$path" "$path"
done
echo "[release] Healthy; current is $target"

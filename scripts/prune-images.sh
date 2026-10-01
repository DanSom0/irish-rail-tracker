#!/usr/bin/env bash
set -euo pipefail

# Keep the running release and the two newest other releases for rollback; remove older
# release images and the unused :latest tag. Never forces removal of an image in use.
# Release bundles in releases/<sha> are kept for exactly the same releases.
IMAGE=ghcr.io/dansom0/irish-rail-tracker
KEEP_PREVIOUS=2

# shellcheck source=scripts/app-dir.sh
source "$(dirname "${BASH_SOURCE[0]}")/app-dir.sh"
current=$(sed -n 's/^IMAGE_TAG=//p' .env | tail -n 1)
if [[ ! $current =~ ^[0-9a-f]{40}$ ]]; then
  echo "[images] IMAGE_TAG in .env is not a full commit SHA; nothing removed" >&2
  exit 1
fi

# Without the image list, bundle retention cannot match it; remove nothing.
if ! images=$(docker image ls "$IMAGE" --format '{{.CreatedAt}}\t{{.Tag}}'); then
  echo "[images] Could not list images; nothing removed" >&2
  exit 1
fi

remove=()
keep=("$current")
kept=0
# Newest first; CreatedAt strings share one timezone, so they sort as text.
while IFS=$'\t' read -r _ tag; do
  if [[ $tag == latest ]]; then
    remove+=("$tag")
  elif [[ $tag =~ ^[0-9a-f]{40}$ && $tag != "$current" ]]; then
    if (( kept < KEEP_PREVIOUS )); then
      keep+=("$tag")
      kept=$((kept + 1))
    else
      remove+=("$tag")
    fi
  fi
done < <(sort -r <<< "$images")

failed=0
for tag in ${remove[@]+"${remove[@]}"}; do
  if docker image rm "$IMAGE:$tag" > /dev/null; then
    echo "[images] Removed $IMAGE:$tag"
  else
    failed=1
  fi
done
echo "[images] Kept $current and $kept previous release(s)"

if [[ -L current ]]; then
  keep+=("$(basename "$(readlink current)")")  # Never remove the active bundle.
fi
for bundle in releases/*/; do
  sha=$(basename "$bundle")
  if [[ $sha =~ ^[0-9a-f]{40}$ && " ${keep[*]} " != *" $sha "* ]]; then
    if rm -rf "releases/$sha"; then
      echo "[releases] Removed releases/$sha"
    else
      failed=1
    fi
  fi
done
# Retention must not fail a deploy that is already running; /status shows disk use.
if (( failed )); then
  echo "[images] Some images or bundles could not be removed; check docker image ls and releases/" >&2
fi

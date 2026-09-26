#!/usr/bin/env bash
set -euo pipefail

# Keep the running release and the two newest other releases for rollback; remove older
# release images and the unused :latest tag. Never forces removal of an image in use.
IMAGE=ghcr.io/dansom0/irish-rail-tracker
KEEP_PREVIOUS=2

cd "$(dirname "${BASH_SOURCE[0]}")/.."
current=$(sed -n 's/^IMAGE_TAG=//p' .env | tail -n 1)
if [[ ! $current =~ ^[0-9a-f]{40}$ ]]; then
  echo "[images] IMAGE_TAG in .env is not a full commit SHA; nothing removed" >&2
  exit 1
fi

remove=()
kept=0
# Newest first; CreatedAt strings share one timezone, so they sort as text.
while IFS=$'\t' read -r _ tag; do
  if [[ $tag == latest ]]; then
    remove+=("$tag")
  elif [[ $tag =~ ^[0-9a-f]{40}$ && $tag != "$current" ]]; then
    if (( kept < KEEP_PREVIOUS )); then
      kept=$((kept + 1))
    else
      remove+=("$tag")
    fi
  fi
done < <(docker image ls "$IMAGE" --format '{{.CreatedAt}}\t{{.Tag}}' | sort -r)

failed=0
for tag in ${remove[@]+"${remove[@]}"}; do
  if docker image rm "$IMAGE:$tag" > /dev/null; then
    echo "[images] Removed $IMAGE:$tag"
  else
    failed=1
  fi
done
echo "[images] Kept $current and $kept previous release(s)"
# Retention must not fail a deploy that is already running; /status shows disk use.
if (( failed )); then
  echo "[images] Some images could not be removed; check docker image ls" >&2
fi

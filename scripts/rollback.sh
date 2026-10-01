#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 || ! $1 =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: rollback.sh <full-40-character-commit-sha>" >&2
  exit 1
fi
# Switches the image and the release bundle (Compose file and scripts) together. Deploy
# keeps the two previous releases, so rolling back to one needs no GHCR token.
echo "[rollback] Rolling back to $1"
exec "$(dirname "${BASH_SOURCE[0]}")/activate-release.sh" "$1"

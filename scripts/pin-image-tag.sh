#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 || ! $1 =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: pin-image-tag.sh <full-40-character-commit-sha>" >&2
  exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
umask 077
temp=$(mktemp .env.XXXXXX)
trap 'rm -f "$temp"' EXIT
sed '/^IMAGE_TAG=/d' .env > "$temp"
printf 'IMAGE_TAG=%s\n' "$1" >> "$temp"
mv "$temp" .env

#!/usr/bin/env bash
set -euo pipefail

# Cron entry point: keep backup.log and one older copy, each about 1 MiB at most.
LOG=backup.log
MAX_BYTES=1048576

cd "$(dirname "${BASH_SOURCE[0]}")/.."
rotated=true
if [[ -f $LOG ]] && (( $(wc -c < "$LOG") > MAX_BYTES )); then
  mv -f "$LOG" "$LOG.1" || rotated=false
fi
exec >> "$LOG" 2>&1
# A rotation failure must not skip the backup itself.
$rotated || echo "[backup] Could not rotate $LOG"
exec ./scripts/backup.sh

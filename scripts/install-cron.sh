#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
trap 'echo "[cron] Installation failed" >&2' ERR
cron_dir=$(mktemp -d)
trap 'rm -rf "$cron_dir"' EXIT

# crontab -l exits 1 when the user has no crontab yet.
if ! LC_ALL=C crontab -l > "$cron_dir/current" 2> "$cron_dir/error"; then
  if ! grep -q 'no crontab for' "$cron_dir/error"; then
    cat "$cron_dir/error" >&2
    exit 1
  fi
fi
sed '/# irish-rail-tracker-backup$/d' "$cron_dir/current" > "$cron_dir/new"
printf '%s\n' '0 3 * * * /opt/irish-rail-tracker/scripts/backup.sh >> /opt/irish-rail-tracker/backup.log 2>&1 # irish-rail-tracker-backup' >> "$cron_dir/new"
crontab "$cron_dir/new"
echo "[cron] Backup scheduled at 03:00 in the server timezone; log: /opt/irish-rail-tracker/backup.log"

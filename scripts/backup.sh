#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck disable=SC1091
source ./.env
: "${BACKUP_BUCKET:?Set BACKUP_BUCKET in .env}"
: "${AWS_REGION:?Set AWS_REGION in .env}"
umask 077
backup_dir=$(mktemp -d)
trap 'rm -rf "$backup_dir"' EXIT
trap 'echo "[backup] Failed; no successful backup confirmed" >&2' ERR
filename="$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
destination="s3://$BACKUP_BUCKET/backups/$filename"

echo "[backup] Creating $filename"
docker compose --env-file .env -f docker-compose.prod.yml exec -T db \
  sh -c 'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  | gzip > "$backup_dir/$filename"

# Upload only a completed dump. No credentials or host AWS config are forwarded;
# the CLI obtains temporary instance-role credentials using IMDSv2.
echo "[backup] Uploading to $destination"
docker run --rm --env "AWS_REGION=$AWS_REGION" \
  --env AWS_EC2_METADATA_V1_DISABLED=true \
  --volume "$backup_dir:/backups:ro" \
  amazon/aws-cli:2 s3 cp "/backups/$filename" "$destination" --only-show-errors
echo "[backup] Uploaded $destination"

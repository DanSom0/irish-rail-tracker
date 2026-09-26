# Operations

[Back to the README](../README.md). These commands are for the person managing the server.

## Provisioning

Terraform creates an Ubuntu EC2 server with a fixed public IP in `eu-west-1`. It also creates a private S3 backup bucket, the server's AWS access role, and a billing alarm in `us-east-1`. Use Terraform **1.16.3** with AWS credentials on your own computer.

First, create a private S3 bucket for Terraform state and an SSH key pair. Run these commands from the repository root:

```sh
cp infra/backend.hcl.example infra/backend.hcl
cp infra/terraform.tfvars.example infra/terraform.tfvars
```

Fill in the state bucket name, public-key path, and billing email in the copies. Then run:

```sh
terraform -chdir=infra init -backend-config=backend.hcl
terraform -chdir=infra plan -out=tfplan
terraform -chdir=infra apply tfplan
terraform -chdir=infra output
```

Review the plan before applying it. **Apply is always manual.** Use the `instance_public_ip` output for `EC2_HOST` and `backup_bucket_name` for `BACKUP_BUCKET`. Enable AWS billing alerts and confirm the subscription email. Never commit `.env`, `backend.hcl`, or `terraform.tfvars`.

Before the first deployment, create `/opt/irish-rail-tracker` on the server, owned by the deploy user (`ubuntu`). Copy `.env.production.example` there as `.env` and replace the placeholders. Set file permissions to `600`, so only its owner can read or write it. The server's startup script installs Docker and Compose.

The monitored station list lives in `app/config.py`. Leave `STATION_CODES` unset in the server's `.env` so web and worker use the list shipped with the image. Remove an older `STATION_CODES` line from the server's `.env` when updating it; set the variable only for an intentional override.

Set these secrets in GitHub's **production** environment: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, and `EC2_KNOWN_HOSTS`. Verify the server's SSH host key before adding it to `EC2_KNOWN_HOSTS`. Allow the repository's workflow token to access its image package in GitHub Container Registry (GHCR). The workflow uses `GITHUB_TOKEN` to sign in; app settings stay in the server's `.env`.

## Deployment details

CI runs on pull requests and pushes to `main`. It checks Python with Ruff, runs pytest against PostgreSQL 16, checks Terraform formatting and validity, and builds the app image. It also checks the production Compose file and shell scripts with ShellCheck.

After CI passes for a push to this repository's `main`, Deploy builds the tested commit. It publishes the image to GHCR with both its full commit SHA and `latest` as tags. Deploys run one at a time. You can also start Deploy manually on `main`.

Deploy copies the production Compose file and scripts to the server. It downloads the SHA-tagged image, starts the services, records the full SHA as `IMAGE_TAG` in the server's `.env`, and schedules backups. Rollback records its selected SHA the same way.

Deploy then runs [`scripts/prune-images.sh`](../scripts/prune-images.sh). It keeps the running image and the two newest other SHA-tagged images for rollback. It removes older release images and the server's `:latest` tag, which Compose no longer uses. It never forces removal of an image a container is using. If a removal fails, the deploy continues and the deploy log says so. Each release image uses about 250 MB of disk.

Each container's Docker log is capped at three 10 MB files (`json-file` driver, set in `docker-compose.prod.yml`). The limits apply once Deploy recreates the containers.

The public `/health` check retries for up to 60 seconds. If it does not return HTTP 200, the job fails. It does not roll back automatically, so `.env` still identifies the image running after `up -d`.

To restart services or apply configuration changes, re-run the **Deploy** workflow on `main`. Never use a bare `docker compose up -d` on the server; the workflow selects and records the deployed image.

Web and worker share one Python 3.12 image. The worker checks stations every five minutes by default. PostgreSQL keeps one row per station, train code, and train date, updating it on each reading.

## Rollback

On the server, choose a previous version that was published to GHCR. Replace the placeholder with its **full 40-character commit SHA**:

```sh
cd /opt/irish-rail-tracker
./scripts/rollback.sh '<previous-full-commit-sha>'
```

Deploy keeps only the two releases before the current one on the server. The script uses the saved image or downloads it, starts the services, records the selected SHA in `.env`, and checks their health. If the image is not on the server and the package is private, sign in to GHCR with read access first. Rollback changes the app version but leaves the database contents in place. The next successful deploy replaces that version.

## Backups

Deploy schedules [`scripts/backup.sh`](../scripts/backup.sh) through cron for **03:00 each night in the server's timezone**. It uses `pg_dump` to save the database as SQL, compresses the file, and uploads it to `s3://<BACKUP_BUCKET>/backups/<UTC-timestamp>.sql.gz`. Cron runs it through [`scripts/nightly-backup.sh`](../scripts/nightly-backup.sh), which appends the output to `/opt/irish-rail-tracker/backup.log`. Once that file is over 1 MiB, it is moved to `backup.log.1` before the next run, replacing any older copy.

S3 encrypts the backups and blocks public access. Backup files expire after seven days. S3 also keeps older versions of replaced files; those expire after one day.

Uploads use `amazon/aws-cli:2.36.49`. The container gets temporary AWS credentials from the server's role. That role can only write files under `backups/` in the backup bucket; it cannot read them.

After setup or a change to the backup image, run `./scripts/backup.sh` on the server. Use a separate AWS identity with S3 read access to confirm that the uploaded file exists.

## Restore a backup

This replaces the current database. The app must be stopped during the restore. Use a trusted backup from this app.

Use your own AWS identity with S3 read access (`s3:GetObject` to download files). Download the chosen backup through the S3 console and securely copy it to the server as `/tmp/restore.sql.gz`. Set its permissions to `600`. The server's role cannot download backups.

Run these commands in Bash on the server. They check the backup, stop the app, and save the current database before replacing it:

```bash
set -euo pipefail
cd /opt/irish-rail-tracker
umask 077
gzip -t /tmp/restore.sql.gz
docker compose --env-file .env -f docker-compose.prod.yml stop web worker
before_restore=$(mktemp -d /tmp/irish-rail-before-restore.XXXXXX)
docker compose --env-file .env -f docker-compose.prod.yml exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  | gzip > "$before_restore/database.sql.gz"
echo "Current database saved to $before_restore/database.sql.gz"
docker compose --env-file .env -f docker-compose.prod.yml exec -T db \
  sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'
gzip -dc /tmp/restore.sql.gz \
  | docker compose --env-file .env -f docker-compose.prod.yml exec -T db \
    sh -c 'psql -X -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose --env-file .env -f docker-compose.prod.yml exec -T db \
  sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*), max(fetched_at) FROM observations;"'
```

If a step fails, leave web and worker stopped and check the error. If the current database was saved successfully, that file holds the previous data. Check the restored row count and last observation time. Then restart the existing containers to keep the same app version:

```sh
docker compose --env-file .env -f docker-compose.prod.yml start web worker
./scripts/healthcheck.sh http://localhost/health
```

Check the dashboard and worker logs. Once the app is working, remove the temporary backup files.

## Monitoring

Set up an external uptime monitor for [the public `/health` page](http://54.228.205.197/health). It should expect HTTP 200. This checks that the web app can reach the database. It does not check whether the worker is collecting new data.

Check the worker logs for `entries_parsed` (rows read for each station) and `observations_saved` (rows saved across all stations). Check `backup.log` (and `backup.log.1` after rotation) for upload failures.

The database, release images and logs share the server's root disk. The [`/status` page](http://54.228.205.197/status) shows how much of it is used. Above 80%, the web app logs `disk_usage_high` when `/status` loads, and the worker logs it on every fetch cycle. Check the worker logs for it. The figure reads the disk from inside the container; it should match `df -h /` on the server. No observation data is ever deleted to free space.

## Environment variables

Copy the settings from [`.env.production.example`](../.env.production.example) into the server's `.env`. Use shell-compatible `KEY=value` lines because the backup script also reads this file in Bash.

| Variable | Example/default | Purpose |
| --- | --- | --- |
| `POSTGRES_DB` | Required | Database name; letters, digits, and underscores. |
| `POSTGRES_USER` | Required | Database user; letters, digits, and underscores. |
| `POSTGRES_PASSWORD` | Required | Random hex password; generate with `openssl rand -hex 32`. |
| `STATION_CODES` | Unset | Optional comma-separated override; defaults to the 20 stations in `app/config.py`, including when set to an empty value. |
| `FETCH_INTERVAL_MINUTES` | `5` | Minutes between station checks. |
| `REQUEST_TIMEOUT_SECONDS` | `15` | Station API request timeout in seconds (worker). |
| `TRAINS_REQUEST_TIMEOUT_SECONDS` | `3` | Train positions request timeout in seconds; kept short because the web server fetches during `/api/trains`. |
| `IRISH_RAIL_API_URL` | Unset | Optional API address; defaults to `http://api.irishrail.ie/realtime/realtime.asmx/getStationDataByCodeXML`. |
| `BACKUP_BUCKET` | Required | Terraform's backup bucket name. |
| `AWS_REGION` | Required (`eu-west-1` here) | Backup bucket region. |
| `IMAGE_TAG` | Managed by Deploy and rollback | Full SHA of the image started by Compose; do not set it in the initial `.env`. |

Compose builds `DATABASE_URL` from the PostgreSQL settings. `WEB_PORT` is local-only (default `8000`); production publishes port `80`.

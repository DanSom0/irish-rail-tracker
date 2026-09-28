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

Deploy copies the commit's production Compose file and scripts to the server as a release bundle (see [Server layout](#server-layout)). It first migrates the database with the new image (see [Database migrations](#database-migrations)); if that fails, the deploy stops and the running release is not touched. It then runs [`scripts/activate-release.sh`](../scripts/activate-release.sh) from that bundle, which downloads the SHA-tagged image and starts the services with the bundle's Compose file. It then checks `http://localhost/health` on the server for up to 60 seconds:

- **Healthy:** it records the full SHA as `IMAGE_TAG` in the server's `.env` and points `current` at the new bundle. Deploy then schedules backups.
- **Unhealthy:** it starts the previous release again from its own bundle and image and leaves `.env` and `current` unchanged. The deploy job fails. The new bundle and image stay on the server for investigation.

Deploy then runs [`scripts/prune-images.sh`](../scripts/prune-images.sh). It keeps the running image and the two newest other SHA-tagged images for rollback. It keeps the release bundles for exactly those releases and removes the others, so every kept image has its Compose file and scripts. It removes older release images and the server's `:latest` tag, which Compose no longer uses. It never forces removal of an image a container is using. If a removal fails, the deploy continues and the deploy log says so. Each release image uses about 250 MB of disk; a bundle is a few kilobytes.

Each container's Docker log is capped at three 10 MB files (`json-file` driver, set in `docker-compose.prod.yml`). The limits apply once Deploy recreates the containers.

The deploy job keeps the server's output. It fails unless that output ends activation with `[release] Healthy; current is <sha>` for the SHA being deployed. A deploy that stops early for any other reason therefore fails too, even if every command it ran succeeded.

After the switch, the workflow checks the public `/health` page for up to 60 seconds. It needs HTTP 200 and `"release"` equal to the deployed SHA; a healthy older release does not pass. If the check fails, the job fails, but the release that passed the server-side check stays current.

`/health` reports the running release as `"release"`: the full commit SHA that production Compose passes to the web container from `IMAGE_TAG`. Releases before this field report no `release`, and a local stack reports `null`.

### Checking a deploy

These checks only read. Replace `<sha>` with the deployed commit's full SHA.

1. In the deploy log, the **Pull and start release** step shows `[migrate] Revision after: <revision> (head)`, then `[release] Healthy; current is <sha>`, then `[cron] Backup scheduled` and `[images] Kept <sha>`.
2. The public health check reports the deployed release:

   ```sh
   curl -s -w ' %{http_code}\n' http://54.228.205.197/health
   ```

   Expect `{"database":"connected","release":"<sha>","status":"ok"}` with HTTP 200. Any other `release` means another release is serving: the deploy did not activate.
3. `/health/ingestion` returns HTTP 200 after the first fetch cycle (see [Monitoring](#monitoring)).
4. On the server, `readlink current` prints `releases/<sha>`, and `grep '^IMAGE_TAG=' .env` prints the same SHA.

To restart services or apply configuration changes, re-run the **Deploy** workflow on `main`. Never use a bare `docker compose up -d` on the server; the workflow selects and records the deployed image.

Web and worker share one Python 3.12 image. Neither creates tables: the schema is owned by the migrations. The worker checks stations every five minutes by default. PostgreSQL keeps one row per station, train code, and train date, updating it on each reading.

## Database migrations

The schema is managed by [Alembic](https://alembic.sqlalchemy.org/) migrations in [`migrations/versions/`](../migrations/versions/). The database records the revision it is at in the `alembic_version` table.

Deploy runs [`scripts/migrate.sh`](../scripts/migrate.sh) from the new release's bundle, before `activate-release.sh`. It downloads the release image if it is not on the server, starts the database if it is not running, and runs `alembic upgrade head` in a one-off container from that image. The deploy log shows the revision before and after (`[migrate] Revision before: ...`). If the migration fails, its transaction is rolled back, the deploy stops, and the running web and worker are not changed.

The migration is applied before the new release's health check, and rollback never downgrades the schema. Every migration must therefore keep working with the release before it: add columns as nullable or with a default, and remove anything the previous release uses only in a later release. Downgrading the baseline is not supported; it refuses because it would drop all observations. To undo a schema change, restore a backup.

The baseline revision (`0001_baseline`) creates the schema the app used to build with `create_all`. It only runs on an empty database. On a database that has tables but no `alembic_version`, it stops with `Database has tables (...) but no Alembic revision`, so it can never be applied over existing data. Such a database must be stamped once instead.

### Stamping the existing production database (one time)

Production was created by `create_all` before migrations existed, so it has the tables but no `alembic_version`. [`scripts/stamp-baseline.sh`](../scripts/stamp-baseline.sh) records the baseline without running it. It refuses to run if `alembic_version` already exists or if the `observations` table, its `uq_observation_train` unique constraint or its indexes are missing. It makes all its checks and changes in one transaction, so a refusal changes nothing.

Do this **before merging the pull request that adds migrations**, in this order. Until the stamp is done, that release's deploy stops at the migration and the running release stays up.

1. **Verify that a backup restores.** On the server, run `./scripts/backup.sh`. Download that backup with your own AWS identity and copy it to the server as `/tmp/restore.sql.gz` (see [Restore a backup](#restore-a-backup)). Restore it into a scratch database, compare it with production, and remove it:

   ```bash
   set -euo pipefail
   cd /opt/irish-rail-tracker
   gzip -t /tmp/restore.sql.gz
   db() { docker compose --env-file .env -f docker-compose.prod.yml exec -T db "$@"; }
   db sh -c 'createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" restore_check'
   gzip -dc /tmp/restore.sql.gz \
     | db sh -c 'psql -X -q -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d restore_check'
   db sh -c 'psql -X -U "$POSTGRES_USER" -d restore_check -c "SELECT count(*), max(fetched_at) FROM observations;"'
   db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*), max(fetched_at) FROM observations;"'
   db sh -c 'dropdb -U "$POSTGRES_USER" restore_check'
   ```

   The restored count and latest time should match production, or be slightly behind it: the worker keeps adding rows. Do not continue until a restore has worked. The scratch copy needs about as much free disk as the database: check `df -h /` first. If a step fails, remove the copy with the last command.

2. **Stamp the baseline.** From your computer, copy the script from the pull request's branch to the server. It is not in a release bundle yet. Then run it on the server:

   ```sh
   scp scripts/stamp-baseline.sh "$EC2_USER@$EC2_HOST:/tmp/stamp-baseline.sh"
   ```

   ```sh
   bash /tmp/stamp-baseline.sh
   cd /opt/irish-rail-tracker
   docker compose --env-file .env -f docker-compose.prod.yml exec -T db \
     sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version;"'
   rm /tmp/stamp-baseline.sh
   ```

   It prints `[stamp] Stamped 0001_baseline`, and the query shows `0001_baseline`. The running release keeps working: its `create_all` only creates missing tables and ignores `alembic_version`.

3. **Merge the pull request.** Deploy runs after CI. In the deploy log, check `[migrate] Revision before: 0001_baseline (head)` and the same revision after it: no migration ran. Then follow [Checking a deploy](#checking-a-deploy) and check the worker logs.

If the deploy stops with `no Alembic revision`, the stamp was not done. Nothing was changed: do step 2, then re-run the **Deploy** workflow on `main`.

A new, empty database (for example, on a replacement server) needs no stamp: the first deploy runs the baseline and creates the schema.

## Server layout

```text
/opt/irish-rail-tracker/
├── .env                        shared by all releases: secrets, settings and IMAGE_TAG
├── backup.log, backup.log.1    nightly backup output
├── releases/
│   ├── <sha>/                  one bundle per kept release
│   │   ├── docker-compose.prod.yml
│   │   └── scripts/
│   └── ...
├── current -> releases/<sha>   the release that last passed /health
├── docker-compose.prod.yml -> current/docker-compose.prod.yml
└── scripts -> current/scripts
```

The server keeps the current release and the two before it, each as a bundle and an image. A release is the pair: `IMAGE_TAG` in `.env` and `current` always name the same SHA. Cron and the commands in this guide use the top-level `docker-compose.prod.yml` and `scripts` links, so they always run the current release's files.

`.env` is not part of a bundle and is not versioned: it holds the database password and other settings that only exist on the server. Rollback does not change it apart from `IMAGE_TAG`. If a release needs a new setting, add it to `.env` before deploying; keep settings an older release still uses until that release can no longer be rolled back to.

Servers deployed before release bundles have the Compose file and scripts as plain files at the top level. The first deploy with bundles copies those files to `releases/<running-sha>/`, the SHA in `IMAGE_TAG`, so that release can be restarted or rolled back to. Once the new release is healthy, it replaces the plain files with the links above. That older bundle's `rollback.sh` predates bundles; while it is current, roll back with a newer bundle's script: `./releases/<newer-sha>/scripts/rollback.sh '<sha>'`.

## Rollback

On the server, list the releases you can roll back to. Each has a bundle and an image:

```sh
cd /opt/irish-rail-tracker
readlink current
ls releases
docker image ls ghcr.io/dansom0/irish-rail-tracker
```

Choose a previous release and replace the placeholder with its **full 40-character commit SHA**:

```sh
./scripts/rollback.sh '<previous-full-commit-sha>'
```

The script switches the image and the release bundle together. It uses the saved image, or downloads it if it is missing, and starts the services with that bundle's Compose file. Services the other release defined but this one does not are removed. It then checks `http://localhost/health`:

- **Healthy:** it records the SHA in `.env` and points `current` at the bundle. Check that `/health` reports that SHA as `release` (releases before the field report none), then the dashboard and the worker logs.
- **Unhealthy:** it restarts the release that was current and exits with an error. Nothing is switched.

**Ingestion readiness after a rollback:** releases before migration `0002_station_polls` do not update the worker heartbeat or station polls. After rolling back to one of them, `/health/ingestion` reports not ready: the older web app does not serve it (HTTP 404), and the heartbeat it left stops advancing, so it passes the age limit. This does not mean the older worker has stopped: check its logs instead. The readiness signal is only authoritative on releases that include `0002_station_polls`. The tables stay in place; when a newer release is deployed again, its worker records fresh polls on its first cycle.

A release without a bundle in `releases/` cannot be rolled back to; the script stops before changing anything. If its image is missing and the package is private, sign in to GHCR with read access first. Rollback changes the app version but leaves the database contents, its schema and `.env` settings in place; it never runs migrations (see [Database migrations](#database-migrations)). The next successful deploy replaces that version.

If a deploy or rollback is interrupted (for example, the SSH connection drops), `current` and `.env` still name the last healthy release, but other containers may be running. Run `./scripts/rollback.sh "$(basename "$(readlink current)")"` to start that release again.

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

If a step fails, leave web and worker stopped and check the error.

The backup includes the schema and its `alembic_version`. If it was taken before the running release's newest migration, bring the schema up to date before starting the app: `./scripts/migrate.sh "$(basename "$(readlink current)")"`. A backup taken before the baseline was stamped has no `alembic_version`; stamp it first with `./scripts/stamp-baseline.sh`. If the current database was saved successfully, that file holds the previous data. Check the restored row count and last observation time. Then restart the existing containers to keep the same app version:

```sh
docker compose --env-file .env -f docker-compose.prod.yml start web worker
./scripts/healthcheck.sh http://localhost/health
```

Check the dashboard and worker logs. Once the app is working, remove the temporary backup files.

## Monitoring

Set up an external uptime monitor for [the public `/health` page](http://54.228.205.197/health). It should expect HTTP 200. This is liveness: it checks that the web app can reach the database. It does not check whether the worker is collecting new data, and deploy and rollback only gate on it.

[`/health/ingestion`](http://54.228.205.197/health/ingestion) is readiness for data collection. The worker records a heartbeat each time it completes a fetch cycle, even when some station polls fail. Its age limit is three fetch intervals, and never less than 15 minutes: 15 minutes at the default `FETCH_INTERVAL_MINUTES=5`, 30 minutes at 10. The same limit decides when a station's board is marked as not current and which stations count as reporting. The page returns:

- **HTTP 200** when the heartbeat is younger than the limit.
- **HTTP 503** when there is no heartbeat, or it has reached the limit: the worker is stopped, stuck or cannot write to the database. It also returns 503 if the database cannot be reached.

The JSON body gives the heartbeat time and age and counts the monitored stations by their latest poll outcome: `ok` (trains returned), `empty` (a valid board with no services, normal overnight), `error` (the poll failed) and `awaiting_first_poll` (no poll recorded yet). Station errors do not cause a 503 on their own. A fresh heartbeat proves the worker is running, not that data is arriving: if every station shows `error`, check the worker logs. A second monitor on this page should expect HTTP 200.

Each station's latest poll is also on the [`/status` page](http://54.228.205.197/status): "OK, N trains", "No services returned", "Update failed; showing data from HH:MM" (the last successful board stays visible, marked as not current) or "Awaiting first poll". A board is also marked as not current when its last successful poll has reached the age limit. Stations whose latest poll failed are left out of the dashboard's current network figures (on-time share, average delay, delayed trains, current delays); the dashboard shows "N stations not updating; excluded", and each station's own page still shows its last board.

Check the worker logs for:

- `station XML parsed` with `entries_parsed`: rows read for each station.
- `station poll succeeded` with `outcome` (`ok` or `empty`) and `train_count`.
- `station poll failed` with `outcome: error` and a short `error_reason`, such as `request timed out`, `connection error`, `HTTP 503`, `invalid XML`, `empty response body` or `database error`. Reasons never include URLs or stack traces; an unexpected error logs `station poll failed unexpectedly` with its traceback.
- `fetch cycle completed` with `observations_saved` and the number of stations by outcome (`stations_ok`, `stations_empty`, `stations_error`).

Check `backup.log` (and `backup.log.1` after rotation) for upload failures.

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

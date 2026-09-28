#!/usr/bin/env bash
set -euo pipefail

# One-time, for a database created by the app's old create_all before migrations existed.
# Records the baseline revision in alembic_version without running it, so that later
# `alembic upgrade head` runs only newer migrations. See docs/operations.md.
#
# It runs before the first release with migrations reaches the server, so it cannot rely
# on a release bundle: copy it to the server and run it with bash. It uses the running
# database container and changes nothing unless every check passes.
cd "${APP_DIR:-/opt/irish-rail-tracker}"
trap 'echo "[stamp] Failed; nothing was changed" >&2' ERR

echo "[stamp] Checking the database in $PWD"
docker compose --env-file .env -f docker-compose.prod.yml exec -T db \
  sh -c 'psql -X -q -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
DO $$
BEGIN
  IF to_regclass('public.alembic_version') IS NOT NULL THEN
    RAISE EXCEPTION 'alembic_version already exists: this database is already stamped or migrated';
  END IF;
  IF to_regclass('public.observations') IS NULL THEN
    RAISE EXCEPTION 'the observations table does not exist: this is not the pre-migration schema';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'public.observations'::regclass
                   AND conname = 'uq_observation_train' AND contype = 'u') THEN
    RAISE EXCEPTION 'unique constraint uq_observation_train is missing from observations';
  END IF;
  IF to_regclass('public.ix_observations_station') IS NULL
     OR to_regclass('public.ix_observations_fetched_at') IS NULL THEN
    RAISE EXCEPTION 'an index on observations (station or fetched_at) is missing';
  END IF;
END
$$;
-- The table `alembic stamp` creates, holding the revision in migrations/versions/0001_baseline.py.
CREATE TABLE alembic_version (
  version_num VARCHAR(32) NOT NULL,
  CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
INSERT INTO alembic_version (version_num) VALUES ('0001_baseline');
SQL
echo "[stamp] Stamped 0001_baseline; the next deploy runs only newer migrations"

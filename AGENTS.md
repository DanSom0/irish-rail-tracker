# Agent guide

Irish Rail delay tracker: Python 3.12, Flask, PostgreSQL 16, and an APScheduler
worker. This project demonstrates SRE/DevOps practice. Read SPEC.md for scope.

## Layout

- `app/`: configuration, XML fetcher, models, routes, templates, and worker.
- `tests/`: pytest checks and recorded XML; never call the live API in tests.
- `infra/`: Terraform for EC2, S3 backups, IAM, and a billing alarm.
- `scripts/`: deploy health check, rollback, backup, and cron installation.
- `.github/workflows/`: CI and production deployment; `docs/postmortems/`: incidents.
- `docker-compose.yml`: local stack; `docker-compose.prod.yml`: SHA-tagged GHCR images.

## Run locally

Copy `.env.example` to `.env` if absent, then `docker compose up --build`.
Dashboard: http://localhost:8000; health: http://localhost:8000/health.

## Checks

Use Python 3.12 and Docker. From the repo root, in a virtual environment:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
docker run --rm -d --name irishrail-test-postgres -p 127.0.0.1:55432:5432 -e POSTGRES_DB=irishrail_test -e POSTGRES_USER=irishrail -e POSTGRES_PASSWORD=irishrail postgres:16-alpine
until docker exec irishrail-test-postgres pg_isready -U irishrail -d irishrail_test; do sleep 1; done
TEST_DATABASE_URL=postgresql+psycopg://irishrail:irishrail@localhost:55432/irishrail_test pytest
ruff check .
docker stop irishrail-test-postgres
```

Tests delete observations and drop the schema: use only the throwaway database.
Deployment tests mock Docker, curl, and cron; they do not verify real image tags.
Terraform 1.16.3 validation (no AWS credentials or state backend required):

```sh
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
```

## Boundaries

- Use conventional commits (`feat:`, `fix:`, `test:`, `ci:`, `docs:`, `chore:`).
- Never run `terraform apply`, `aws`, or `ssh` commands. Operator runbooks are in README.md.
- Never commit `.env`, `backend.hcl`, or `terraform.tfvars`.
- Keep work within the requested PR; migrations and dashboard polish are later work.

## Irish Rail XML quirks

- Handle the default XML namespace for both `objStationData` rows and child fields.
- Strip trailing whitespace from `Traincode`; parse `Traindate` like `16 Sep 2026`.
- For terminating trains, `Schdepart` is `00:00`; use `Scharrival` instead.
- Tara Street is `TARA`, not `TARAJ`; keep per-station `entries_parsed` logging.
- `Late` is minutes; retain the last polled value, not a confirmed final arrival delay.

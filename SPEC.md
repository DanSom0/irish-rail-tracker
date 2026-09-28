# Irish Rail Delay Tracker: Spec

Portfolio project for SRE/DevOps internships. The repo itself (structure,
commits, PRs, Actions, README) should look like a professional engineer's.

## Status
The planned PRs 1 to 7 below are merged, and the service runs in
production at https://dublinrailtracker.duckdns.org. Later work came from a read-only
audit (26 Sep 2026) and from incidents; since then:
- Alembic migrations own the schema (baseline `0001_baseline`, then
  `0002_station_polls`).
- Every station poll is recorded (ok, empty or error), with a worker
  heartbeat and /health/ingestion.
- Deploys use versioned release bundles, migrate before activation, and
  must confirm the new release on /health.
- The live map shows station delays and last-reported train positions.
Remaining work is tracked in GitHub issues, not in this file.

## Stack
Python 3.12, Flask, PostgreSQL 16, SQLAlchemy, Alembic, Docker +
docker-compose, pytest, GitHub Actions, GitHub Container Registry (GHCR),
Terraform, single AWS EC2 instance (Ubuntu).

## Data source
Irish Rail realtime API (XML, no key needed):
https://api.irishrail.ie/realtime/realtime.asmx
- getStationDataByCodeXML for a configurable list of stations. The
  default list of 20 Dublin-area stations lives in app/config.py
  (`DEFAULT_STATION_CODES`); STATION_CODES overrides it, and production
  sets no override.
- getCurrentTrainsXML for train positions: fetched by the web app for
  the live map, with a short timeout (3 s by default) and a 60-second
  per-process cache that also caches failures. Positions are the last
  station a train passed, not GPS.
- The "Late" field is delay in minutes.
- Response has a default XML namespace; Traincode has trailing
  whitespace; Traindate format is "16 Sep 2026"; terminating trains
  have Schdepart "00:00" (use Scharrival instead).
- Poll every 5 minutes. Handle timeouts, empty responses and bad XML
  gracefully with logging; never crash the worker.
- Log entries parsed per station so a silent zero is visible.

## Application
- Two services from one image: `web` (Flask + Gunicorn) and `worker`
  (APScheduler fetcher). Plus `db` (Postgres).
- Alembic migrations (migrations/) own the schema; the app never creates
  tables. Each migration must keep working with the previous release.
- Store each observation: station, train code, train date, origin,
  destination, scheduled time, delay minutes, fetched_at.
- Record each station's latest poll (station_polls: ok, empty or error,
  with train count and a short error reason) and the worker's last
  completed cycle (worker_heartbeat). A successful poll saves its
  observations and its record in one transaction with one shared
  timestamp; a station's current trains are exactly that poll's trains.
- Dedup: unique on (station, train code, train date); upsert the latest
  delay so each train is counted once in averages.
- Dashboard (server-rendered, minimal CSS): current delays table, average
  delay by station, average delay by route, last updated time.
- /health returns 200 with DB connectivity status and the running
  release SHA as JSON (liveness).
- /health/ingestion returns 200 while the worker's heartbeat is younger
  than max(15 minutes, 3 x FETCH_INTERVAL_MINUTES), else 503, with
  station counts by poll outcome (readiness).
- Structured JSON logging to stdout.
- All config via environment variables; include .env.example.
  No secrets committed anywhere.

## Tests
- pytest with fixture XML files; tests must never call the live API.
- One behaviour per test. Cover: XML parsing edge cases, empty and
  malformed responses, timeouts, DB upsert/dedup, /health (connected
  and disconnected), dashboard route.
- CI runs tests against a Postgres service container.

## Infrastructure (Terraform)
- infra/ directory, AWS provider, pinned versions.
- Remote state in an S3 bucket (created manually once; bucket name via
  backend config, not hardcoded).
- Provisions:
  - EC2 (Ubuntu, t3.micro) with Elastic IP
  - Security group: 22 open with key-only auth, 80 and 443 open
  - Key pair from my existing public key
  - Private S3 bucket for database backups, 7-day lifecycle expiry
  - IAM instance role allowing the instance to write to that bucket only
  - CloudWatch billing alarm (us-east-1 provider alias)
- User data installs Docker and the Compose plugin.
- Outputs: public IP (used as the EC2_HOST deploy secret), backup
  bucket name.
- CI runs terraform fmt -check and terraform validate. CI never runs
  apply; I run apply manually.

## Deployment
- docker-compose.prod.yml: uses the GHCR image (no build), restart
  policies, persistent Postgres volume. Caddy is the only service
  that publishes ports (80 and 443): it serves
  https://dublinrailtracker.duckdns.org with a Let's Encrypt
  certificate kept in a named volume, proxies to web, and redirects
  all HTTP, including the bare IP, to that URL.
- deploy.yml: on push to main, after CI passes. Build image, push to
  ghcr.io tagged with the commit SHA (and `latest`, which the server
  does not use), copy that commit's Compose file, Caddyfile and
  scripts to the server as a release bundle, run `alembic upgrade head`
  with the new image, then start the release. It becomes current only
  after the server-side /health check passes; otherwise the previous
  release is restarted. The job fails unless the server reports the new
  release as current and the public /health reports its SHA.
- The server's .env records the running SHA as IMAGE_TAG; restarts go
  through the Deploy workflow, never a bare `docker compose up -d`.
- Rollback: scripts/rollback.sh starts a previous release's bundle with
  its image. It never runs or reverses migrations.
- Backups: nightly pg_dump on the instance (cron), uploaded to the
  backup bucket via the instance role. No AWS keys on the instance.
- Use a GitHub Environment called "production" for deploy secrets.

## GitHub
- Repo layout: app/, tests/, infra/, scripts/, docs/, .github/workflows/,
  docker-compose.yml, docker-compose.prod.yml, Dockerfile, .env.example,
  README.md, AGENTS.md.
- ci.yml: on PRs and pushes to main. Ruff, pytest against PostgreSQL,
  Terraform fmt and validate, ShellCheck, a production Compose config
  check, and a Docker image build (no push).
- Dependabot for pip, Docker, GitHub Actions and Terraform. Ignore minor
  and major updates to the python Docker base image. Group GitHub
  Actions updates into one weekly PR.
- PR template (what changed, how tested).
- README badges: CI status, deploy status.
- Conventional commits (feat:, fix:, test:, ci:, docs:, chore:, style:).
- One PR at a time, in this order:
  1. Core app (done)
  2. Tests + CI + Dependabot + PR template (done)
  3. Terraform infrastructure; Docker build in CI; Dependabot config changes (done)
  4. Production compose, deploy.yml, rollback script, backups (done)
  5. README, AGENTS.md, postmortem (done)
  6. Alembic migrations (baseline matching the existing schema, safe
     to run against the production database) (done)
  7. Dashboard polish and delay patterns (done)

## README
Project summary, live link, Mermaid architecture diagram (Terraform ->
EC2 + S3; GitHub Actions -> GHCR -> EC2 -> web/worker/db -> Irish Rail
API), local setup in under 5 commands, how CI/CD works, how to provision
infra, how to roll back, backups, uptime monitoring (external monitor on
/health), env var table, and a short "design decisions" section
(including why terraform apply is manual and what the delay figure
actually measures: the last reading before a train leaves the board).

## Postmortems
docs/postmortems/: one blameless postmortem per incident (summary,
impact, timeline, root cause, detection, fix, lessons, follow-ups),
using facts from git history and PRs. The first covered the XML
namespace bug where the worker reported successful fetches but stored
zero rows; the README links them all.

## PR 7: Dashboard polish and delay patterns
Server-rendered, no JS frameworks.
- Clean, mobile-friendly CSS; colour-code delays (on time / minor / major)
- Station filter via query parameter
- Prominent "data as of X minutes ago"
- Status section: last successful fetch per station
- Heatmap of average delay by day of week x hour (Europe/Dublin time,
  bucketed by scheduled time, not fetched_at), server-rendered SVG,
  filterable by station
- Show sample size per cell; grey out cells with too few trains

## Constraints
Write complete, working code. Keep it simple and conventional. No
Kubernetes, frontend frameworks, auth, or features beyond this spec.

## Manual steps
At the end of each relevant PR, give me numbered steps for anything I
must do myself:
- AWS: IAM user/credentials for Terraform, S3 state bucket, SSH key,
  terraform init/plan/apply, confirming the billing alarm email
- GitHub: production environment secrets, GHCR permissions, branch
  protection on main requiring CI to pass, repo description, topics,
  pinning the repo
- External uptime monitor on /health

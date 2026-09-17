# Irish Rail Delay Tracker: Spec

Portfolio project for SRE/DevOps internships. The repo itself (structure,
commits, PRs, Actions, README) should look like a professional engineer's.

## Status
- PR 1 (core app): merged
- PR 2 (tests + CI): merged
- Next: PR 3

## Stack
Python 3.12, Flask, PostgreSQL 16, SQLAlchemy, Alembic, Docker +
docker-compose, pytest, GitHub Actions, GitHub Container Registry (GHCR),
Terraform, single AWS EC2 instance (Ubuntu).

## Data source
Irish Rail realtime API (XML, no key needed):
http://api.irishrail.ie/realtime/realtime.asmx
- getStationDataByCodeXML for a configurable list of stations.
  Defaults: CNLLY, PERSE, HSTON, TARA, MHIDE.
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
- Store each observation: station, train code, train date, origin,
  destination, scheduled time, delay minutes, fetched_at.
- Dedup: unique on (station, train code, train date); upsert the latest
  delay so each train is counted once in averages.
- Dashboard (server-rendered, minimal CSS): current delays table, average
  delay by station, average delay by route, last updated time.
- /health returns 200 with DB connectivity status as JSON.
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
  - Security group: 22 open with key-only auth, 80 open
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
  policies, web on port 80, persistent Postgres volume.
- deploy.yml: on push to main, after CI passes. Build image, push to
  ghcr.io tagged with commit SHA and latest, SSH into EC2, log the host
  into GHCR, pull the new image, `docker compose up -d`, then curl
  /health and fail the job if it doesn't return 200.
- Rollback: scripts/rollback.sh redeploys a given previous image SHA.
- Backups: nightly pg_dump on the instance (cron), uploaded to the
  backup bucket via the instance role. No AWS keys on the instance.
- Use a GitHub Environment called "production" for deploy secrets.

## GitHub
- Repo layout: app/, tests/, infra/, scripts/, docs/, .github/workflows/,
  docker-compose.yml, docker-compose.prod.yml, Dockerfile, .env.example,
  README.md, AGENTS.md.
- ci.yml: on PRs and pushes to main. Ruff, pytest, and a Docker image
  build (no push) so base-image and server changes are tested.
- Dependabot for pip, Docker, GitHub Actions and Terraform. Ignore minor
  and major updates to the python Docker base image. Group GitHub
  Actions updates into one weekly PR.
- PR template (what changed, how tested).
- README badges: CI status, deploy status.
- Conventional commits (feat:, fix:, test:, ci:, docs:, chore:, style:).
- One PR at a time, in this order:
  1. Core app (done)
  2. Tests + CI + Dependabot + PR template (done)
  3. Terraform infrastructure; Docker build in CI; Dependabot config changes
  4. Production compose, deploy.yml, rollback script, backups
  5. README, AGENTS.md, postmortem
  6. Alembic migrations (baseline matching the existing schema, safe
     to run against the production database)
  7. Dashboard polish and delay patterns (only after a few weeks of
     production data)

## README
Project summary, live link, Mermaid architecture diagram (Terraform ->
EC2 + S3; GitHub Actions -> GHCR -> EC2 -> web/worker/db -> Irish Rail
API), local setup in under 5 commands, how CI/CD works, how to provision
infra, how to roll back, backups, uptime monitoring (external monitor on
/health), env var table, and a short "design decisions" section
(including why terraform apply is manual and what the delay figure
actually measures: the last reading before a train leaves the board).

## Postmortem
docs/postmortems/2026-09-zero-observations.md: the XML namespace bug
where the worker reported successful fetches but stored zero rows.
Cover impact, why it went undetected, detection, fix, and the
per-station parse count added to prevent recurrence. Blameless format.

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
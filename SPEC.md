Build a portfolio project for me: an Irish Rail delay tracker. It will be
on my CV for SRE/DevOps internships, so the repo itself (structure,
commits, PRs, Actions, README) should look like a professional engineer's.

## Stack
Python 3.12, Flask, PostgreSQL 16, SQLAlchemy, Docker + docker-compose,
pytest, GitHub Actions, GitHub Container Registry (GHCR), Terraform,
single AWS EC2 instance (Ubuntu).

## Data source
Irish Rail realtime API (XML, no key needed):
http://api.irishrail.ie/realtime/realtime.asmx
- Use getStationDataByCodeXML for a small configurable list of major
  stations (e.g. Connolly, Pearse, Heuston, Tara Street, Malahide).
  The "Late" field is delay in minutes.
- Poll every 5 minutes. Handle timeouts, empty responses and bad XML
  gracefully with logging; never crash the worker.

## Application
- Two services from one image: `web` (Flask + Gunicorn) and `worker`
  (scheduled fetcher using APScheduler). Plus `db` (Postgres).
- Store each observation: station, train code, origin, destination,
  scheduled time, delay minutes, fetched_at.
- Dedup: unique on (station, train code, train date); upsert the latest
  delay so each train is counted once in averages.
- Dashboard (server-rendered, minimal CSS): current delays table, average
  delay by station, average delay by route, last updated time.
- /health returns 200 with DB connectivity status as JSON.
- Structured logging to stdout.
- All config via environment variables; include .env.example.
  No secrets committed anywhere.

## Tests
- pytest with fixture XML files; tests must never call the live API.
- Cover: XML parsing, delay calculation, DB inserts/dedup, /health,
  dashboard route.
- CI runs tests against a Postgres service container.

## Infrastructure (Terraform)
- infra/ directory, AWS provider, pinned versions.
- Remote state in an S3 bucket (created manually once; bucket name via
  backend config, not hardcoded).
- Provisions: EC2 (Ubuntu, t3.micro), security group (22 open with
  key-only auth, 80 open), key pair from my existing public key,
  Elastic IP, CloudWatch billing alarm (us-east-1 provider alias).
- User data installs Docker and the Compose plugin.
- Outputs: public IP (used as the EC2_HOST deploy secret).
- CI runs terraform fmt -check and terraform validate. CI never runs
  apply; I run apply manually.

## GitHub
- Repo layout: app/, tests/, infra/, .github/workflows/,
  docker-compose.yml, Dockerfile, .env.example, README.md, AGENTS.md.
- Workflows:
  1. ci.yml: on every PR and push. Lint (ruff) + pytest. Must pass.
  2. deploy.yml: on push to main, after CI passes. Build image, push to
     ghcr.io tagged with commit SHA and latest, SSH into EC2, log the EC2
     host into GHCR, pull the new image, `docker compose up -d`, then curl
     /health and fail the job if it doesn't return 200.
- Use a GitHub Environment called "production" for deploy secrets.
- Add Dependabot config for pip, Docker, GitHub Actions and Terraform.
- Add a PR template (what changed, how tested).
- README badges: CI status, deploy status.
- Deliver work as separate PRs with conventional commit messages
  (feat:, fix:, ci:, docs:), in this order:
  1. Flask app, models, fetcher, dashboard, docker-compose (runs locally)
  2. Tests + ci.yml + Dependabot + PR template
  3. Terraform infrastructure (infra/)
  4. deploy.yml + production compose file
  5. README + AGENTS.md
    6. Dashboard polish + delay patterns (after deploy)

## README
Project summary, live link placeholder, Mermaid architecture diagram
(Terraform -> EC2; GitHub Actions -> GHCR -> EC2 -> web/worker/db ->
Irish Rail API), local setup in under 5 commands, how CI/CD works,
how to provision infra, env var table, and a short "design decisions"
section (including why terraform apply is manual).

## PR 6: Dashboard polish and delay patterns
Only after the app is deployed and has collected a few weeks of data.
Keep it server-rendered with no JS frameworks.
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
At the end, give me numbered steps for everything I must do myself:
- AWS: IAM user/credentials for Terraform, S3 state bucket, SSH key,
  terraform init/plan/apply, confirming the billing alarm email
- GitHub: required secrets for the production environment, GHCR
  permissions, branch protection on main requiring CI to pass,
  repo description, topics, and pinning the repo on my profile
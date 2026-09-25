# Live map browser review

A Playwright review of the live map's train layer, run by hand before changing
`app/static/map.js`, `app/templates/map.html` or the map styles. It is not part of
CI: it needs real browsers and waits for the map's 60-second refresh.

It runs every scenario in Chromium and WebKit, at 1280x800 and 375x812, in light
and dark mode. The scenarios cover:

- the show/hide toggle and grouping at different zooms;
- badge expansion, keyboard selection and focus return;
- escaping of feed text and the mobile bottom sheet;
- empty, stale, error and failed-refresh states for trains and stations;
- trains leaving the feed while their panel is open: a single train, part of a
  group, and a whole group.

Everything runs against a disposable stack. Never point it at the normal local
volume or production; `seed.py` refuses any database not named `*_review`.

## Run

From the repository root, with the virtual environment from AGENTS.md active:

```sh
docker run --rm -d --name irishrail-review-postgres -p 127.0.0.1:55433:5432 -e POSTGRES_DB=irishrail_review -e POSTGRES_USER=irishrail -e POSTGRES_PASSWORD=irishrail postgres:16-alpine
until docker exec irishrail-review-postgres pg_isready -U irishrail -d irishrail_review; do sleep 1; done
export DATABASE_URL=postgresql+psycopg://irishrail:irishrail@localhost:55433/irishrail_review
export IRISH_RAIL_TRAINS_API_URL=http://127.0.0.1:8765/getCurrentTrainsXML
python -m tests.browser.seed
python tests/browser/upstream.py &
flask --app wsgi run --port 8001 &
(cd tests/browser && npm ci && npx playwright install chromium-headless-shell webkit && npm run review)
```

The review exits non-zero if any check fails. Screenshots go to `tests/browser/shots/`.
Set `ONLY=webkit-375-dark` (browser, width and scheme) to run a single combination.
Station readings count as recent for 10 minutes, so re-run `python -m tests.browser.seed`
if the stack has been up longer than that.

Afterwards, stop the background jobs and run `docker stop irishrail-review-postgres`.

# Successful fetches but zero observations

## Summary

During initial development, the worker logged successful station fetches but stored no observations. The parser did not handle the API's default XML namespace. The fix added namespace-aware parsing and per-station `entries_parsed` logging.

## Impact

Fetched station data did not reach PostgreSQL, leaving the dashboard without observations from those fetches. The history does not establish the number of missed observations or an incident duration; this occurred before the production deployment was added.

## Timeline

Dates below are commit dates; detection has no separately recorded timestamp.

| Date | Evidence/event |
| --- | --- |
| 2026-09-16 | [`e5f9fae`](https://github.com/DanSom0/irish-rail-tracker/commit/e5f9fae4dfde531f44f9644fbccf93b6c71253e3) introduced the fetcher with unqualified XML lookups. |
| Before the parsing fix | A direct PostgreSQL query showed zero stored rows despite successful-fetch logs. |
| 2026-09-16 | [`7cdf848`](https://github.com/DanSom0/irish-rail-tracker/commit/7cdf848e21dac2b09dfb46e35a47325fa5df46f3) added namespace-aware parsing and per-station parse counts. |
| 2026-09-16 | [`fea1029`](https://github.com/DanSom0/irish-rail-tracker/commit/fea1029dc8b5a574e6172d35bef4f90c87a5ce17) merged namespaced XML fixtures, parser regression tests, and PostgreSQL upsert tests. |
| 2026-09-16 | The new logging later revealed a separate wrong station code; [`c2b21a4`](https://github.com/DanSom0/irish-rail-tracker/commit/c2b21a44aa47c0c85a22d0ce5c0adaf1be6e8dfc) changed `TARAJ` to `TARA` in configuration defaults. |

## Root cause

`ElementTree` represents namespaced tags as `{namespace}tag`. The original `root.findall(".//objStationData")` matched none of the API's namespaced rows; child lookups also used unqualified names. Parsing returned an empty list, and the upsert function correctly did nothing for an empty input.

## Detection

Querying PostgreSQL directly exposed the missing data. The `station fetch succeeded` message was emitted after a successful HTTP response and parsing, even when `observations_received` was zero. That message alone did not prove ingestion; `/health` only checked database connectivity. Per-station parse counts were not yet logged, and parser regression tests arrived after the fix.

## Fix

[`app/fetcher.py`](../../app/fetcher.py) now discovers rows using the document namespace and reads child fields by their local tag names. It logs `station` and `entries_parsed` before converting rows to observations. The same fix also normalised train dates and used `Scharrival` for terminating trains with `Schdepart` equal to `00:00`.

## Lessons

- HTTP success and database connectivity do not establish that useful data was stored.
- Fixtures need to preserve the real XML structure, including its default namespace.
- Per-station counts help distinguish a feed-wide parser failure from a station configuration error, as the later Tara correction showed.

## Follow-ups

- Completed: namespace-aware parsing and parse-count logging in `7cdf848`.
- Completed: the namespaced fixture and parser/upsert checks in [`tests/`](../../tests/) via `fea1029`.
- Completed: correct Tara Street defaults to `TARA` in `c2b21a4`.
- Proposed: check per-station data freshness alongside `/health`; an empty board by itself is not proof of a fault.

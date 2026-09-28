# Departed trains shown as current delays

## Summary

Current delay views showed trains that had already left a station's board. At first, "current" meant any reading from the last 30 minutes. It was then narrowed to each station's latest stored readings, if under 10 minutes old. Even then, a board that became empty, or a failed fetch, left the older trains on display, because neither was stored. The final fix records every station poll and defines "current" as exactly the trains from the latest successful poll.

## Impact

- The homepage delay list, station boards and current network figures could include departed trains:
  - from 23 September, for up to 30 minutes;
  - from later that day, for up to 10 minutes.
- Current averages and "6+ min late" counts could include those trains.
- A failed fetch could not be told apart from a quiet station, and `/health` stayed green if every fetch failed.
- Daily figures were not affected: they count each train's latest reading for the day by design.
- How often users saw departed trains is not recorded.

## Timeline

Dates are from git and GitHub.

| Date | Event |
| --- | --- |
| 2026-09-23 | [`b39d86b`](https://github.com/DanSom0/irish-rail-tracker/commit/b39d86bce6b015a2de3d05ed3d90d7b4be0008c2) (#24) built the current views from readings in the last 30 minutes. Its description noted that empty fetches were not stored. |
| 2026-09-23 | [`2475fac`](https://github.com/DanSom0/irish-rail-tracker/commit/2475fac40ed41c00c7494c7e8472fe7282b5930c) (#28) limited current views to each station's latest stored poll, if at most 10 minutes old. A regression test had shown trains from 30 and 7 minutes earlier after a newer poll. |
| 2026-09-26 | An audit of `222c200` reproduced the remaining gap ([#41](https://github.com/DanSom0/irish-rail-tracker/issues/41)). After a valid empty feed, `/api/network` still showed 1 current train, and `/health` still returned `ok`. |
| 2026-09-28 | [`0e72aae`](https://github.com/DanSom0/irish-rail-tracker/commit/0e72aae9dcf0f10ccfc43eed4c4b3991dd2b5388) (#68) added station poll records and a worker heartbeat. Because of [a deploy fault](2026-09-deploys-without-activation.md), it went live with [`110494b`](https://github.com/DanSom0/irish-rail-tracker/commit/110494bf08cc866090600e107be3b30057071968) at 18:53 UTC. |

## Root cause

The app stored only train observations. A poll that returned no trains, and a poll that failed, both left nothing behind. The views could only guess the latest poll from the newest stored train, so they could not tell "the board is now empty" from "we have not heard from this station". Time windows limited how long departed trains stayed, but could not remove them at the right moment.

## Detection

- A regression test written for #28 showed the first case: older trains stayed after a newer poll.
- The audit for #41 showed the second, with an empty feed applied to a disposable database.
- No alert covered either case.

## Fix

- **#28:** limited current views to the latest stored poll, for at most 10 minutes.
- **[#68](https://github.com/DanSom0/irish-rail-tracker/pull/68):**
  - **Poll record:** every station poll now leaves a record in `station_polls`, as `ok`, `empty` or `error`.
  - **Shared timestamp:** a successful poll saves its trains and its record in one transaction, with one shared timestamp.
  - **Current trains:** a station's current trains are exactly the trains with its latest successful poll's timestamp.
  - **Empty board:** an empty board clears the station.
  - **Failed poll:** the last good board stays, marked as not current. Stations whose latest poll failed are left out of the current network figures.
- **Worker heartbeat:** `/health/ingestion` returns HTTP 503 when the worker has not finished a cycle within its age limit. The limit is three fetch intervals, and never less than 15 minutes.

## Lessons

- "No data" needs its own record. Otherwise an empty result and a failure look the same.
- A time window can hide a stale-data problem, but it cannot fix it.
- Liveness (`/health`) and data freshness (`/health/ingestion`) are different questions and need separate checks.

## Follow-ups

- Completed: #28, #68, and the release check that makes sure such fixes actually go live (#70).
- Open: [#50](https://github.com/DanSom0/irish-rail-tracker/issues/50) shows freshness on the map markers.
- Open: [#69](https://github.com/DanSom0/irish-rail-tracker/issues/69) excludes boards older than the age limit from current figures.

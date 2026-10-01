# Deploys reported success without activating

## Summary

On 28 September 2026, two deploys passed but never switched production to their release. Each deploy migrated the database and then stopped without an error. The public health check still passed, because the older release was healthy. Production served the release from #65 for about 80 minutes while the pipeline reported #66 and then #68 as deployed. The fix stopped Docker Compose from reading the deploy script's input, made the deploy check for activation, and added the running release to `/health`.

## Impact

- From about 17:33 to 18:53 UTC, production ran [`175c183`](https://github.com/DanSom0/irish-rail-tracker/commit/175c183e4216e637a16ba8faa438c27826d9acc8) (#65) instead of the releases the deploys reported.
- The Alembic change (#66) and the station poll outcomes (#68) were not live. `/health/ingestion` returned HTTP 404.
- Migration `0002_station_polls` was applied to the production database during the #68 deploy. The older release kept working against it, because the migration only added tables.
- The deploys also skipped `install-cron.sh` and image pruning. The backup cron entry from earlier deploys was still in place.
- No data loss or outage is recorded.

## Timeline

Times are from the Deploy workflow logs and GitHub, in UTC.

| Time (28 Sep) | Event |
| --- | --- |
| 17:31 | [#66](https://github.com/DanSom0/irish-rail-tracker/pull/66) merged. It added the migrate step to the deploy's remote script. |
| 17:33 | Deploy of [`e799634`](https://github.com/DanSom0/irish-rail-tracker/commit/e79963454cf32e56f94b31c9725adef2814c91b7) logs `[migrate] Revision after: 0001_baseline (head)` and nothing after it. The job passes. |
| 18:28 | [#68](https://github.com/DanSom0/irish-rail-tracker/pull/68) merged. |
| 18:30 | Deploy of [`0e72aae`](https://github.com/DanSom0/irish-rail-tracker/commit/0e72aae9dcf0f10ccfc43eed4c4b3991dd2b5388) applies migration 0002 and logs nothing after it. The job passes. |
| After 18:30 | While preparing documentation, the live map still showed the pre-#68 wording and `/health/ingestion` returned 404. The deploy logs had no `[release]` lines after the migrate step; the #65 deploy had them. |
| 18:50 | [#70](https://github.com/DanSom0/irish-rail-tracker/pull/70) merged. |
| 18:53 | Deploy of [`110494b`](https://github.com/DanSom0/irish-rail-tracker/commit/110494bf08cc866090600e107be3b30057071968) logs `[release] Starting 110494b… (previous: 175c183…)` and `[release] Healthy; current is 110494b…`. The public check reports `release 110494b…`. |

## Root cause

The deploy runs its server-side steps by sending a script over SSH to `bash -s`, so bash reads the script from standard input. `migrate.sh` runs `docker compose run -T web alembic …`, and `docker compose run` reads standard input too. It read the rest of the deploy script. Bash then reached the end of its input and exited with status 0.

Nothing failed, so nothing stopped the job. The last check was an HTTP 200 from the public `/health`, and the older release still returned 200.

## Detection

The pipeline did not detect it. It was found by looking at the live site: new pages were missing, and the deploy log had no activation lines.

The deploy tests replaced `docker` with a stub and ran each script directly, never through `bash -s`. The stub did not read standard input, so the tests could not show this failure.

## Fix

[#70](https://github.com/DanSom0/irish-rail-tracker/pull/70):

- **`</dev/null`:** Compose calls in `migrate.sh` and `activate-release.sh` read from `/dev/null`. `rollback.sh` runs `activate-release.sh`, so it's covered too.
- **Function wrapper:** the remote script is one function, `deploy() { … }; deploy "$@"`. Bash reads a function in full before running it, so a step that reads standard input can no longer consume the steps after it.
- **Activation check:** the deploy keeps the server's output. It fails unless that output contains `[release] Healthy; current is <sha>` for the SHA being deployed.
- **Release SHA on `/health`:** `/health` reports `"release"`, set from the deployed `IMAGE_TAG`. The public check requires it to match the deployed SHA, so a healthy older release no longer passes.
- **Tests:** they run the workflow's real deploy step with a stub `ssh` that runs the remote command locally, and a stub `docker` that reads standard input on every Compose call. Removing both fixes reproduces this incident in the tests.

## Lessons

- A health check proves that some release is healthy. It does not prove that the new release is the one serving. The check needs to name the release.
- A script fed to `bash -s` shares its input with every command it runs. Any command that reads input can end the script early, and the exit status is still 0.
- A deploy should confirm that each step it depends on actually ran, not only that nothing failed.

## Follow-ups

- Completed: the fixes above in #70. `docs/operations.md` now has read-only [deploy checks](../operations.md#checking-a-deploy) that use the `release` field.
- Open: [#54](https://github.com/DanSom0/irish-rail-tracker/issues/54) (build the image once and deploy the tested digest) would tie the release to the image itself rather than to a deploy setting.
- Open: [#67](https://github.com/DanSom0/irish-rail-tracker/issues/67), missing failure message when Compose fails during activation.

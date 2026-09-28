# A manual restart with "latest" reverted production

## Summary

A manual restart on the server started an older image than the one deployed. The server's `.env` still said `IMAGE_TAG=latest`, and the server's local `latest` image was out of date. The fix recorded the deployed commit SHA in `.env` after every deploy and rollback. The documentation now says to restart through the Deploy workflow.

The revert itself was reported by the operator. The repository records the cause and the fix, but not when the restart happened or how long the older image ran.

## Impact

- Production ran an older release than the one the last deploy had started, until a deploy replaced it.
- The start time, duration and the exact older version are not recorded.
- No data loss is recorded. The database is separate from the image and was not changed.

## Timeline

Dates are from git and GitHub.

| Date | Event |
| --- | --- |
| 2026-09-21 | [`3f80963`](https://github.com/DanSom0/irish-rail-tracker/commit/3f80963524f9bb218220bfee23a8706906531ae3) (#19) added production deployment. Deploy set `IMAGE_TAG` to the commit SHA only for its own `docker compose up`, while `.env.production.example` set `IMAGE_TAG=latest`. |
| Before 2026-09-24 | A manual restart on the server started the older `latest` image (reported by the operator). |
| 2026-09-24 | [`6d7205b`](https://github.com/DanSom0/irish-rail-tracker/commit/6d7205b40ae284e6ab5ed2bcb8b044981790ab7b) (#31) pinned the deployed SHA in the server's `.env`. |

## Root cause

Deploy exported the new commit SHA as `IMAGE_TAG` only while it ran `docker compose up -d`. The server's `.env` kept `IMAGE_TAG=latest` from the example file. Any later Compose command on the server read `.env` and used `latest`.

Deploy pulled only the SHA-tagged image, so the server's local `latest` tag was never updated. It still pointed at an older image, and that image was started.

## Detection

The operator noticed that production was running an older version after the restart. Nothing in the app reported which release was running.

## Fix

[#31](https://github.com/DanSom0/irish-rail-tracker/pull/31):

- `scripts/pin-image-tag.sh` writes the full SHA as `IMAGE_TAG` in `.env` once Compose has started it. Deploy and rollback both use it.
- `.env.production.example` no longer sets `IMAGE_TAG`.
- `docs/operations.md` says to restart or change settings by re-running the Deploy workflow, never with a bare `docker compose up -d`.
- A regression test failed while `.env` still had `IMAGE_TAG=latest` after a SHA-tagged start, and passed after the fix.

Later changes made the same fault harder to repeat:

- [#63](https://github.com/DanSom0/irish-rail-tracker/pull/63) removes the server's `latest` tag during image pruning.
- [#65](https://github.com/DanSom0/irish-rail-tracker/pull/65) records a release as its image and Compose file together.
- [#70](https://github.com/DanSom0/irish-rail-tracker/pull/70) reports the running release on `/health`.

## Lessons

- A floating tag on a server means whatever that server last pulled, not the latest build.
- The server's configuration must name the running version, so that any restart starts that version again.
- The app should report its own version, so that a mismatch is visible without logging in to the server.

## Follow-ups

- Completed: SHA pinning (#31), removing `latest` (#63), release bundles (#65) and the release on `/health` (#70).
- Open: [#54](https://github.com/DanSom0/irish-rail-tracker/issues/54), build the image once and deploy the tested digest.

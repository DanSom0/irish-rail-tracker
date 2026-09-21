# Backup image tag did not exist

## Summary

The first version of `backup.sh` used `amazon/aws-cli:2`, a non-existent image tag. A manual run on the server before the first scheduled backup exposed the failure. The fix pinned the published full version `2.36.49`.

## Impact

The manual backup attempt could not upload its database dump to S3. The script exits on failure and removes its temporary local dump, so that attempt did not produce a retained backup. The failure was detected before the first scheduled run; no missed scheduled run or application outage is established by the record.

## Timeline

Dates below are commit dates; the manual run has no separately recorded timestamp.

| Date | Evidence/event |
| --- | --- |
| 2026-09-21 | [`3f80963`](https://github.com/DanSom0/irish-rail-tracker/commit/3f80963524f9bb218220bfee23a8706906531ae3) added production deployment, nightly backups, and tests with mocked Docker commands. |
| Before the first scheduled backup | Running the backup manually on the server exposed the unavailable `amazon/aws-cli:2` tag. |
| 2026-09-21 | [`08f4f38`](https://github.com/DanSom0/irish-rail-tracker/commit/08f4f38d43f37977bcb9206a94988fadc47ac5bd) replaced the tag with the full version `2.36.49`. |

## Root cause

The script assumed a major-version Docker tag existed. The AWS CLI container is only invoked after `pg_dump` and compression, so the unavailable image prevented the upload stage from running.

## Detection

A manual server run exercised the real Docker image reference. The unit tests had passed because [`tests/test_deploy_scripts.py`](../../tests/test_deploy_scripts.py) replaces `docker` with a local stub. Those tests check dump/upload failure handling and compressed contents, but do not contact a registry or pull an image. CI's application-image build does not build or pull the separate AWS CLI backup image.

## Fix

[`scripts/backup.sh`](../../scripts/backup.sh) defines `AWS_CLI_VERSION=2.36.49` and runs `amazon/aws-cli:$AWS_CLI_VERSION`. The full version makes the image reference explicit and leaves one value to change for future updates.

## Lessons

- Mocked command tests verify script behaviour, not the existence of external images.
- A successful application deploy and `/health` check do not verify that backups can run.
- Running a new scheduled job manually can expose integration failures before its first unattended execution.

## Follow-ups

- Completed: pin the AWS CLI image to `2.36.49` in `08f4f38`.
- Proposed: verify the published image manifest whenever the backup image version changes.
- Proposed: after backup changes, run the script on the host and verify the S3 object with operator read access; periodically rehearse a restore.

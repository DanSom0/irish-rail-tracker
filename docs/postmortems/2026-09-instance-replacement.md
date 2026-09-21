# EC2 instance replaced during a root volume change

## Summary

The goal was to grow the server's root volume to 20 GB. A plan on the fix branch showed `0 to destroy`. The PR merge step then failed, but a chained command sequence continued. The fix was not on `main` when a new plan and apply ran there, replacing the EC2 instance.

## Impact

- The site was down for about 30 minutes.
- Roughly one hour of production data was lost.
- The server's `.env`, deploy key authorization, and cron setup were lost.
- The Elastic IP was preserved.

## Timeline

Events are listed in order. Exact timestamps are not recorded here.

| Stage | Event |
| --- | --- |
| Goal | Grow the root volume to 20 GB. |
| Fix branch | Added lifecycle `ignore_changes` for `associate_public_ip_address`. The plan showed `0 to destroy`. |
| Merge attempt | The PR merge step failed. The chained command sequence continued, leaving `main` without the fix. |
| Apply on main | A new plan and apply ran on `main` and replaced the EC2 instance. |
| Recovery | Merged the fix and applied it in place. Restored deploy key access, updated `EC2_KNOWN_HOSTS`, recreated `.env`, and redeployed through `workflow_dispatch`. |
| Verification | A reboot test confirmed that services restarted automatically. |

## Root cause

AWS reports `associate_public_ip_address` as `true` when an Elastic IP is attached. The configuration set it to `false`. That difference forces Terraform to replace the instance. Adding lifecycle `ignore_changes` for this field prevented replacement in the plan on the fix branch.

The command sequence continued after the PR merge failed. As a result, `main` still lacked the fix when it was planned and applied. The earlier `0 to destroy` result belonged to a different branch.

The server also depended on manual setup for `.env` and deploy key authorization. Code alone could not recreate those settings, so recovery needed manual steps.

## Detection

The `0 to destroy` plan described the fix branch, not the code later applied from `main`. The incident record does not specify an alert source or exact detection time.

## Fix

The fix was merged and applied in place. Deploy key access was restored, the `EC2_KNOWN_HOSTS` secret was updated, and `.env` was recreated. The app was redeployed by starting the Deploy workflow through `workflow_dispatch`.

A reboot test afterwards confirmed that the services restarted automatically.

## Lessons

- Read the plan generated from the exact branch being applied, immediately before applying it.
- Do not chain an apply after steps that can fail. A failed merge must stop the sequence.
- Server setup was not fully reproducible from code. Missing `.env` and deploy key authorization required manual recovery.

## Follow-ups

- Proposed: make server setup reproducible, including deploy key authorization and `.env`, using user data or AWS Systems Manager (SSM) Parameter Store.

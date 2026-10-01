# HTTPS release deployed before port 443 was open

## Summary

On 28 September 2026, the deploy of the HTTPS change (#73) switched production to Caddy before the security group allowed port 443. Caddy redirected every plain HTTP request to `https://dublinrailtracker.duckdns.org`, and connections to port 443 from outside timed out, so the site was unreachable. The deploy's public check failed, but the release stayed active, as designed, because the check on the server had passed. The site recovered when the Terraform change that opened port 443 was applied. A manual deploy then confirmed the release from outside. The fix adds a check that port 443 is reachable before a deploy builds or changes anything.

## Impact

- From 23:37 UTC, when the release was activated, until the security group change was applied (between 23:38 and 23:43 UTC; the exact time was not recorded), the site was unreachable for visitors:
  - `http://` URLs, including the bare IP, returned HTTP 308 to `https://dublinrailtracker.duckdns.org`.
  - `https://` connections timed out.
- `/health` and `/health/ingestion` were also unreachable from outside.
- The worker kept collecting data; it does not depend on inbound ports. No data loss is recorded.

## Timeline

Times are from GitHub and the Deploy workflow logs, in UTC.

| Time (28 Sep) | Event |
| --- | --- |
| 23:34 | [#73](https://github.com/DanSom0/irish-rail-tracker/pull/73) merged. Its rollout order was to apply the Terraform change (ingress on TCP 443) first, then merge. The Terraform change had not been applied yet. |
| 23:35 | CI passed for [`86cc3c2`](https://github.com/DanSom0/irish-rail-tracker/commit/86cc3c25b638209247eb21a8fa39e8bab12de25c), and Deploy run [36498850226](https://github.com/DanSom0/irish-rail-tracker/actions/runs/36498850226) started. |
| 23:36:57 | `[release] Starting 86cc3c2… (previous: b065847…)`. Caddy starts; port 80 was open, so it could obtain its certificate. |
| 23:37:09 | The server-side check, through Caddy on `127.0.0.1:443`, passes: `[release] Healthy; current is 86cc3c2…`. Production now serves HTTPS only. |
| 23:37:09–23:38:10 | The public check gets no connection: 15 attempts end with `curl: (28) Failed to connect to dublinrailtracker.duckdns.org port 443 after 2001 ms`. The job fails. |
| 23:38–23:43 | The security group change is applied with Terraform, opening port 443. |
| 23:43 | Deploy run [36499464548](https://github.com/DanSom0/irish-rail-tracker/actions/runs/36499464548) is started manually for the same commit. |
| 23:44:22 | The public check passes: `[health] HTTP 200: healthy; release 86cc3c2…`. |

## Root cause

Merging to `main` deploys automatically. The infrastructure is applied separately and by hand. #73 needed both, in a set order: open port 443, then deploy. That order was written in the pull request description, and nothing in the pipeline checked it. The pull request was merged first, so the release went out before the port was open.

The server-side health check could not catch it. To cover TLS and the proxy, it connects to Caddy on the server itself (`curl --resolve …:443:127.0.0.1`). That path does not cross the security group, so it passed. The public check did cross the security group and failed, but by design it runs after activation and does not roll back. A release that is healthy on the server stays current when only the public check fails.

The same release also redirected all plain HTTP to HTTPS. Before it, the site was served on port 80. So the closed port turned into an outage for every visitor, not only for the new HTTPS URL.

## Detection

The Deploy workflow's public health check failed within about a minute of activation and marked the run as failed. The curl errors named the cause: a connection timeout on port 443.

## Recovery

The Terraform change from #73 was applied. It changed only the security group, in place (plan: 0 to add, 1 to change, 0 to destroy). Caddy was already running with its certificate, so the site answered over HTTPS as soon as the port opened. A manual Deploy run for the same commit then checked the public site and passed. Nothing was rolled back.

## Fix

This pull request:

- **Port check before the deploy:** the Deploy job's first step opens a TCP connection to port 443 on the site from the runner. It fails the deploy if the connection does not complete within 10 seconds, which is what a security group without the port causes. It runs before the image is built or the server is touched, so the running release stays active. A refused connection passes: the port is open, and a release from before HTTPS has nothing listening on it until Caddy starts.
- **Runbook:** [HTTPS](../operations.md#https) now says that changes affecting ports 80 or 443 must be applied with Terraform before the pull request that needs them merges.

## Lessons

- When a change needs infrastructure and a deploy in a set order, and only the deploy is automatic, the order has to be checked by the pipeline, not only written down.
- A health check from inside the server shows that the release works. It cannot show that users can reach it, because it does not cross the firewall.
- A change that stops serving on one port and starts on another turns a problem with the new port into a full outage. Check that the new path works from outside before the old one is removed.

## Follow-ups

- Completed: the port check and runbook note above.
- Open: the port check only covers port 443. A change that needs other infrastructure before it deploys still depends on the rollout order in its pull request.

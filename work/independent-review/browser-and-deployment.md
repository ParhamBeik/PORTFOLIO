# Browser, CI, deployment, and live state

* The recovered task's deploy target is `26ee30a0360bb72cb3ba7796faf030b0db62b449`.
* Local `main`, `origin/main`, and `git ls-remote origin refs/heads/main` resolved to that SHA.
* GitHub Actions run `34702621654` completed successfully for that SHA: backend, audit,
  frontend, and deployment jobs were terminal-successful.
* Read-only production probes returned HTTP 200: liveness `ok`, readiness database/cache true,
  and prices fresh (87 seconds against a 900-second threshold at recheck).
* The live health response carried HSTS, CSP, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, and strict referrer policy.
* The connected browser is currently authenticated as an owner account. Its portfolio, operator
  route, and visible financial data were inspected read-only; no live data was altered during this review.

Local uncommitted auth/operator/mail changes are not deployed and must not be described as live.

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

## Release verification — 2026-09-13

* Reviewed code commit `c87eee3ee16460efe81bcb570acbf3cbe1b63df8` was fast-forwarded to
  local `main`, `origin/main`, and the remote `main` ref.
* GitHub Actions run `34740102439` completed successfully: PostgreSQL backend suite,
  migration check, shell lint, restore drill, dependency audits, frontend lint/unit/Vitest/build,
  response-header assertions, Lighthouse, and VPS deployment were all terminal-successful.
* Its hosted PostgreSQL suite reported `1155 passed, 3 skipped, 4 warnings` in 158.50s.
  The skips are the known platform BLAS-symbol probe; the warnings are upstream optimizer
  deprecations, not test failures.
* Read-only SSH independently found VPS `HEAD` at `c87eee3`, healthy backend, frontend,
  database, Redis, MinIO, live/archive workers, and beat. The recent backend/worker log scan
  found no error, exception, traceback, or critical line.
* Public probes returned `ok`, database/cache readiness true, and fresh prices (33 seconds,
  below the 900-second threshold). The deployed registration contract returned
  `registration_open: true` and `self_service_reset: false`, accurately hiding a reset flow
  that cannot deliver mail in the current environment.

Browser limitation: the connected browser holds an owner account with real data. The new
sell-all behavior is covered by the released component and API tests; a live mutation was not
performed because confirming it would write a real ledger sale.

# Evidence Ledger

Audit date: 2026-09-12 (Asia/Tehran)

## Baseline

| Item | Observed result | Status |
|---|---|---|
| Live URL | `https://portfolio-89-106-206-4.sslip.io/` | PASS |
| Existing browser session | Existing in-app tab reused; signed-in owner/staff account exposes the operator console | PASS for read-only admin scope |
| Local checkout | Branch `feat/multi-user-auth-mvp`, `HEAD=26ee30a`; 14 modified/untracked feature files preserved | OBSERVED |
| VPS checkout | `/opt/apps/portfolio-repo/portfolio-saas`, `main`, `HEAD=26ee30a0360bb72cb3ba7796faf030b0db62b449`; one pre-existing untracked backup script | OBSERVED |
| Local/live code parity | Live is on `HEAD`; local auth/operator changes are uncommitted and therefore not deployed | FINDING |
| VPS containers | Backend, frontend, live/archive workers, database, Redis, beat, and MinIO reported healthy | PASS |
| Database migrations | Production `showmigrations --plan` showed applied migrations through current heads | PASS |
| Public liveness | `/api/health/` returned HTTP 200 `{"status":"ok"}` | PASS |
| Public readiness | `/api/health/ready/` returned HTTP 200 with database/cache checks true | PASS |
| Price freshness | `/api/health/prices/` returned HTTP 200, latest price age 8 seconds, threshold 900 seconds | PASS |
| Edge headers | HSTS, CSP, `nosniff`, `DENY`, and strict referrer policy observed on health responses | PASS |
| Browser console | No error/warning entries observed on sampled routes and auth validation | PASS (sampled) |
| Anonymous API boundary | Registration `200`; `/me/`, accounts, and admin overview `401` without credentials | PASS |

## Browser evidence

### Signed-in member session

- Root rendered holdings, live values, allocation, ledger links, risk/correlation/diversification panels, and empty liabilities state.
- Root values were internally plausible at the sample: total `24,150,700 T`, USD equivalent `$103.25`, Gold `99%`, Cash `1%`, two live API holdings.
- `/ledger`, `/family`, `/comparison`, `/prices`, `/optimal`, `/universe`, `/privacy`, and `/terms` rendered after hydration.
- `/onboarding` redirected to `/` because the account already has holdings.
- `/best-overall` redirected to `/universe`; `/breakdown` redirected to `/family`; unknown route redirected to `/`.
- `/ops` was not accessible: the account menu identified the user as `Member` and showed `Operator console Not available`.

### Signed-in admin/operator session

- The user-provided signed-in session was inspected in place; the account menu identified an owner/staff identity and showed `Operator console Available`.
- `/ops` rendered the read-only Operations center. Overview, Live pipeline, Warehouse, Tables, Asset, Jobs & health, and Codal panels were inspected without invoking refresh, retry, reset, repair, workflow execution, or deletion controls.
- Read-only operator evidence included 7 users, 4 users with portfolios, 6 portfolios, 26 holdings, 2 online workers, current price freshness, archive backlog/failure counters, provider quota usage, disk usage, workflow outcomes, and Codal dormant status. Individual account emails and portfolio-sensitive values were not copied into the artifact.
- The admin session produced no browser console error or warning entries during the operator inspection. No admin portfolio, holding, liability, ledger, account, or other sensitive record was changed.

### Anonymous session

- Signed out through the visible account menu; no portfolio data was deleted or changed.
- Landing page rendered sign-in, create-account tab, forgot-password control, and disabled submit until valid input.
- Invalid signup input produced field-level invalid email, weak-password, and password-mismatch feedback; create-account remained disabled.
- Invalid synthetic sign-in produced `Invalid email address or password. Please check your credentials and try again.` with no console warnings.
- Public `/privacy` and `/terms` rendered with a working back-to-sign-in link.
- `/reset-password` rendered its token-dependent form with disabled submit and no console warnings.

## Current findings

### F-001 — Protected deep link keeps protected URL while rendering sign-in

- Severity: HIGH (authentication/deployment correctness)
- Reproduction: signed out; navigate directly to `/ledger`.
- Expected: redirect to `/login?next=%2Fledger` (or equivalent visible login route) while retaining the safe return path.
- Observed live: the sign-in card rendered while the browser URL and document title remained `/ledger`.
- Evidence: live AX state showed the sign-in form under URL `/ledger`; a later settled `/login` route test redirected correctly only when explicitly navigated.
- Local context: the dirty local `App.jsx` contains a `LoginRedirect` and explicit `/login` route, but those changes are not deployed to the VPS.
- Next action: deploy and re-run the deep-link/auth-return regression test; do not claim live fixed from local code alone.

### F-002 — Direct `/signup` deep link opens sign-in mode on live deployment

- Severity: MEDIUM (auth UX/deployment parity)
- Reproduction: signed out; navigate directly to `/signup`.
- Expected: create-account mode is selected immediately.
- Observed live: URL remained `/signup`, title was `Sign in — Holdings`, and `Sign in` tab was selected; clicking `Create account` then showed the correct form.
- Local context: the dirty local `App.jsx`/`Auth.jsx` changes add explicit `/signup` routing and `initialMode`, but are not deployed.
- Next action: deploy and re-run direct `/signup` and post-login return-path checks.

### F-003 — Live registration-status contract predates local mail-availability contract

- Severity: MEDIUM (deployment/configuration parity)
- Evidence: live `GET /api/auth/registration/` returned only `{"registration_open":true}`;
  local dirty backend returns `registration_open` plus `self_service_reset`, and local
  frontend now fail-closes the reset link until the latter is present.
- Impact: the current live UI and local dirty UI do not represent the same password-reset
  capability. Deploying only one side of this change would mislead users about recovery.
- Next action: deploy backend and frontend together, then verify the status payload, reset
  affordance, and actual relay behavior in the target environment.

### F-004 — Holding quantity zero removes the only holding without visible confirmation

- Severity: MEDIUM (destructive UX/validation boundary)
- Reproduction: in the isolated synthetic account, open holding edit, change the quantity from `12` to `0`, and save.
- Observed live: the save succeeded, the holding disappeared, and the account returned to the onboarding screen. No inline validation or confirmation explained that zero would remove the holding.
- Scope: synthetic test account only; the admin account was not used for this mutation.
- Follow-up: restore a positive synthetic holding, then test the explicit delete control separately with an action-time confirmation.

### F-005 — Deleting a holding-reversal ledger row resurrects the holding

- Severity: HIGH (cross-entity state integrity)
- Reproduction: delete the synthetic gold holding, which created a `Sold` ledger reversal; then delete that synthetic `Sold` ledger row.
- Observed live: the sold reversal disappeared and the previously deleted gold holding reappeared in the portfolio. This means ledger-row deletion changes holding state and is not isolated to ledger history.
- Scope: synthetic test account only; no admin data was involved.
- Follow-up: retain as a required regression case for any future ledger/holding deletion change.

### F-006 — Money-in transaction rejected by cash-balance validation

- Severity: OBSERVATION (behavior needs product clarification)
- Reproduction: in the synthetic account, choose `Money in`, enter `500,000 T`, select the current date, and save.
- Observed live: the wizard remained open and displayed `Insufficient cash balance`; no ledger row was created.
- Interpretation: this may be an intentional rule for the selected transaction type, but it conflicts with the plain-language `Money in` label and should be clarified or covered by a dedicated UX test.

## Local verification

| Check | Result | Boundary |
|---|---|---|
| Backend Ruff 0.16.6 | All checks passed | Local dirty checkout |
| Backend pytest | 1,153 passed, 1 skipped, 4 dependency deprecation warnings | Local PostgreSQL, current dirty checkout |
| Frontend ESLint | Passed | Local dirty checkout |
| Frontend standalone unit tests | 26 passed | Local dirty checkout |
| Frontend Vitest | 47 passed across 9 files | Local dirty checkout |
| Frontend production build | Passed; charts bundle emits a non-failing >500 kB warning | Local dirty checkout |

## Isolated synthetic-account evidence

- Account creation succeeded through the public signup UI in a separate tab; generated credentials were not written to disk or this ledger.
- Initial portfolio and holding creation succeeded: a prefixed portfolio was created with one cash holding, and the root dashboard read back the expected quantity and live value.
- Refresh/navigation persistence succeeded: revisiting `/` retained the prefixed portfolio and holding.
- Holding edit succeeded: the holding name changed to a prefixed synthetic name and quantity changed from `10` to `12`; the recalculated value updated accordingly.
- Zero-quantity boundary reproduced F-004; the synthetic holding was removed and the app returned to onboarding. The holding was restored before the remaining CRUD and deletion checks.
- Restored the synthetic cash holding at quantity `12` and added a second synthetic gold holding through the multi-step add-asset flow, including a dated note.
- Liability CRUD succeeded: created a prefixed bank-loan liability at `100,000 T`, verified it in net worth, edited the name and balance to `125,000 T`, and verified the revised total.
- Ledger CRUD succeeded for the synthetic gold purchase: created it through the add-transaction wizard, edited quantity `2`→`3` and its note, and verified the recalculated row value. A synthetic cash-deposit attempt was rejected with `Insufficient cash balance` and made no row.
- Ledger reverse was exercised on a synthetic USD row; the row set changed without touching the admin account. Explicit ledger deletion was subsequently exercised on a synthetic reversal row.
- The holdings delete mode was opened read-only; the UI explicitly states that deletion removes the holding and reverses its ledger entries. The per-record synthetic gold deletion was then executed under the action-time confirmation gate.
- Explicit synthetic holding deletion succeeded and reduced the dashboard to the remaining cash holding; deleting its generated reversal then restored the gold holding, reproducing F-005.
- Synthetic-account authorization boundary succeeded: navigating directly to `/ops` redirected to `/` and exposed no operator-console controls.
- Synthetic profile CRUD succeeded: first/last name changed to prefixed test values and the UI reported `Name saved.`
- Synthetic account deletion succeeded through the password-plus-`DELETE` confirmation gate. A subsequent login attempt with the generated credentials returned `Invalid email address or password`, proving the account was no longer usable.
- Duplicate signup protection succeeded: submitting a generated strong password for the existing admin email returned `An account with this email address already exists. Try signing in instead.` No admin data or credentials were changed.
- Before the synthetic account deletion, a final read-only check of the original admin tab still showed the owner identity, `Full administrative access`, and `Operator console Available`; no admin record or portfolio data was mutated.

## Timing and responsiveness

- Anonymous route readiness (navigation to a settled accessibility tree): `/` 433–1,142 ms,
  `/privacy` 313–508 ms, `/reset-password` 291–511 ms, three observations each.
- Public health HTTPS responses: `/api/health/` 370–404 ms, `/api/health/ready/`
  308–377 ms, `/api/health/prices/` 377–399 ms, three observations each.
- At an explicit 390×844 viewport, the sign-in card had no horizontal overflow:
  document/body scroll width equaled 390 px. At 1280×900, the terms page also had no
  horizontal overflow: document/body scroll width equaled 1280 px.
- No official page-performance budget was present in the audit instructions or observed
  application surface, so these values are reported as measurements rather than pass/fail
  thresholds. Browser page-performance entries were unavailable through the evaluation API.

The two local test edits were limited to (1) including the new
`self_service_reset` field in the auth-status fixture and waiting for that
asynchronous control, and (2) accepting the established DRF `401`/`403`
unauthenticated refresh contract. No production code was changed by this audit.

## Blockers and limits

- Admin/operations read-only audit is complete for the connected owner/staff session; no admin mutation was attempted.
- Because the in-app browser shares session state across tabs, the admin session was logged out through the visible account menu to reach public signup; this changed session state only and did not modify admin data. The original admin tab was later observed signed out after the synthetic-account lifecycle; restoring that session would require the user to authenticate again.
- Synthetic signup was completed in a separate browser tab using a generated `@portfolio.local` address and generated strong password held only in the browser session; no credentials were written to the artifact. The synthetic account was later deleted and failed a subsequent login attempt.
- Password-reset email delivery was not tested as a real send; production history indicates SMTP remains an operational boundary and the audit did not send mail.
- Browser performance timing API was unavailable through the browser evaluation surface; route-level timing measurements and VPS/request evidence are recorded without inventing pass/fail thresholds.

## Exit assessment

- Coverage denominator: 18 user-facing inventory rows. All 18 have an evidence-backed result; 15 were fully executed (12 clean passes plus 3 passes with documented findings), 2 password-reset rows are blocked by SMTP/token safety limits, and 1 account-menu row is partial because password change/export were intentionally not executed.
- The remaining live issues are explicit findings F-001 through F-006 with severity, reproduction, impact, and next action; none is unexplained.
- Local verification, deployed commit, VPS health, live HTTP probes, browser evidence, and current dirty-worktree boundaries are separated in this ledger. No deployment or production source mutation was performed by this audit.
- The audit is complete with documented limitations. The live deployment still differs from the dirty local auth/mail changes and requires a coordinated deployment plus re-verification before those live findings can be closed.

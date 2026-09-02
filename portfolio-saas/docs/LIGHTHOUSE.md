# Lighthouse 100 across all four categories

Audit, fixes and enforcement, 2026-08-31. Companion to
[`PRODUCT-EVALUATION.md`](PRODUCT-EVALUATION.md).

## Status: achieved and gated

```
$ npm run test:lighthouse
==> response headers on the document ......... ok (CSP, HSTS, nosniff, Referrer-Policy, COOP)
==> response headers on a hashed asset ....... ok (gzip, immutable, CSP)
==> lighthouse (3 runs)
{'performance': 100, 'accessibility': 100, 'best-practices': 100, 'seo': 100}
{'performance': 100, 'accessibility': 100, 'best-practices': 100, 'seo': 100}
{'performance': 100, 'accessibility': 100, 'best-practices': 100, 'seo': 100}

FCP 0.3s · LCP 0.3s · TBT 0ms · CLS 0 · Speed Index 0.3s
```

Stable across three runs against the **real production image**, not the `dist/` folder —
see "How it is enforced" for why that distinction is the whole point.

**That number is the sign-in page**, which is what an unauthenticated Lighthouse run
reaches: every other route is behind a JWT (`App.jsx:53`). The authenticated routes are
covered separately, below.

### The authenticated routes (2026-09-02)

`npm run test:lighthouse:auth`, against production with a real session:

| Route | Perf | A11y | Best practices | SEO | FCP | LCP | TBT | CLS |
|---|:--:|:--:|:--:|:--:|--|--|--|--|
| `/` (Dashboard) | **100** | **100** | **100** | **100** | 0.0 s | 1.1 s | 70 ms | **0** |
| `/ledger` | **100** | **100** | **100** | 63 | 0.9 s | 1.0 s | 0 ms | 0.009 |
| `/optimal` | **100** | **100** | **100** | 63 | 0.9 s | 0.9 s | 0 ms | **0** |

**SEO 63 on the app routes is the correct state, not a gap.** Every authenticated route is
`Disallow`ed in `robots.txt`, and the whole 37-point deduction is the single audit "Page is
blocked from indexing". These pages return the sign-in shell to a crawler; there is nothing
to index. Scoring SEO 100 on them would mean inviting crawlers into the app. The public
sign-in page still enforces `seo: 1.0` through `lighthouserc.json`, which is where SEO
means anything.

**`/optimal` scored 92 on one run in three**, with FCP, LCP, TBT and CLS identical across
all three. The whole difference was Speed Index, which moves with CPU contention on the
machine running the audit. It is not a property of the page; treat a single sub-100 Speed
Index reading as noise and re-run.

### What closed the last of it: reserving height for async content

All three routes carried layout shift, and all three had the same cause — a loading state
shorter than the content that replaced it. Two lessons worth keeping:

- **Lighthouse names the element that MOVED, not the one that caused it.** The Dashboard
  reported its chart grid shifting, so the first fix reserved height on the two chart
  cards. The score did not move: those cards were not growing, they were being *pushed*, by
  a hero row whose `Async` reserved nothing at all.
- **Reserve what the loaded body actually takes, not what the chart takes.** The trend and
  allocation cards reserved their 260 px chart height while the loaded body is the chart
  plus the note under it (382 px measured for the allocation donut). Every number in those
  fixes was measured against production at the viewport Lighthouse audits (412 px), not
  guessed — and the hero needed a responsive reservation rather than `Async`'s inline
  `minHeight`, because its tiles stack under `sm` (184 px stacked, 94 px at 1350 px).

Dashboard CLS 0.062 → 0, Ledger 0.05 → 0.009, My Optimal 0.05 → 0.

The sections below record what was wrong, since the reasoning is what stops it regressing.

Lighthouse v12 scores **four** categories — Performance, Accessibility, Best Practices,
SEO. The PWA category was retired, so "100 on all benchmarks" means those four.

Two things to settle before the work starts, because they change what "100" means:

1. **Which URL is audited.** Every route except `/privacy` and `/terms` is behind auth
   (`App.jsx:53`), so an unauthenticated Lighthouse run scores the **sign-in page**. That
   page is cheap and 100 is very reachable. The Dashboard is the page users actually live
   on, it is 596 KB of echarts heavier, and Performance 100 there is the genuinely hard
   target. **Score both, gate on both**, or the number is theatre.
2. **What serves the HTML.** Lighthouse audits the document served by
   **`frontend/nginx.conf`**, not by Django. The `SECURE_HSTS_*` / `SECURE_CONTENT_TYPE_NOSNIFF`
   / `SECURE_REFERRER_POLICY` settings at `config/settings.py:446-454` are real and correct,
   but they only apply to `/api/` and `/admin/` responses. The HTML document and every JS
   asset get **no security headers and no compression** today.

---

## Current state, by category

### Performance — the two blockers are compression and echarts

**`nginx.conf` enables no compression.** The file is a bare `server {}` block in
`conf.d/`; there is no `gzip on`, no `gzip_types`, and no brotli module. The stock
`nginx:alpine` image ships gzip commented out. So unless the upstream `vps-edge` Caddy is
compressing on the way through — **which is not configured in this repository and must be
verified** — assets go over the wire raw:

| Asset | Raw | ~gzip | ~brotli |
|---|---:|---:|---:|
| `charts-*.js` (echarts) | **596 KB** | ~180 KB | ~150 KB |
| `index-*.js` (React + router + Sentry) | 232 KB | ~75 KB | ~65 KB |
| `index-*.css` | 36 KB | ~7 KB | ~6 KB |
| `Ops-*.js` | 60 KB | ~18 KB | ~15 KB |

One line of nginx config removes roughly 70% of the transfer. **Do this first** — it is the
highest ratio of score to effort in the entire plan, and it also improves every real user's
experience, which the score is only a proxy for.

Verify the current state before assuming:

```bash
curl -sI -H 'Accept-Encoding: gzip, br' https://<host>/assets/<hashed>.js \
  | grep -i 'content-encoding\|cache-control\|content-length'
```

**echarts is 610 KB — and it is already as small as it goes.** An earlier draft of this
document recommended switching to modular `echarts/core` imports. **That was wrong: it is
already done.** `components/charts.jsx:2-22` imports `graphic, init, use` from
`echarts/core`, registers exactly five chart types and five components, and its own comment
records the migration — the barrel import shipped 1,049 KB and the modular build cut it to
roughly a quarter. There is no further tree-shaking to harvest.

So compression is not one lever among several, it is the lever: 610 KB → **202 KB gzipped**,
measured by the Vite build. Two secondary options remain if the Dashboard audit (item 8)
later needs them — deferring chart hydration below the fold, and splitting `HeatmapChart` +
`VisualMapComponent` into their own chunk since only the correlation panel uses them — but
neither is needed for the sign-in page, which never loads the chunk at all.

**Already good, and worth not breaking:** system font stack only (`index.css:103`) so there
is zero web-font cost and no FOIT/FOUT; the only image is a 799-byte SVG; content-hashed
assets with `expires 1y; Cache-Control: public, immutable` (`nginx.conf:28-31`); route-level
code splitting.

**Watch:** `React.StrictMode` (`main.jsx:17`) double-renders in dev only — it does not affect
a production Lighthouse run, so leave it alone.

### Accessibility — measured failures, not guesses

The foundation is strong: a skip-to-content link (`Shell.jsx:70`), `:focus-visible` rings
(`index.css:118`), `role="status"` / `role="alert"` on live regions, `scope="col"` and an
`sr-only` `<caption>` on every table (`ui.jsx:286-291`), `aria-label` on icon buttons,
`aria-pressed` on tab buttons, `role="dialog"` + `aria-modal` on `Modal`.

**But the status-colour tokens fail WCAG AA as text, and they are used as text everywhere.**
Computed from `index.css` (WCAG 2.x relative luminance, AA small text = 4.5:1):

| Token used as text | on `bg` | on `panel` | on `panel-2` |
|---|---:|---:|---:|
| **Dark mode** | | | |
| `--c-critical` | **4.02** ✗ | **3.77** ✗ | **3.43** ✗ |
| `--c-accent` | 5.31 ✓ | 4.97 ✓ | 4.53 ✓ |
| **Light mode** | | | |
| `--c-warn` | **1.72** ✗ | **1.83** ✗ | **1.65** ✗ |
| `--c-serious` | **2.48** ✗ | **2.64** ✗ | **2.37** ✗ |
| `--c-good` | **3.15** ✗ | **3.35** ✗ | **3.01** ✗ |
| `--c-accent` | **4.15** ✗ | **4.42** ✗ | **3.96** ✗ |
| `--c-critical` | 4.51 ✓ | 4.80 ✓ | **4.31** ✗ |

This is not a corner case. `toneClass` maps `good` → `text-[var(--c-good)]` and `critical` →
`text-[var(--c-critical)]` (`ui.jsx:22-28`), and `Delta` (`ui.jsx:38`) uses it for **every
signed number in the app** — every P&L cell, every Δ weight, every Δ value. So in light mode
every profit figure is 3.15:1, and in dark mode every loss figure is 4.02:1. `Badge` has the
same problem (`ui.jsx:84-87`), as do the inline error paragraphs
(`Ledger.jsx:182`, `Auth.jsx:193`, `App.jsx:57`).

Button fills fail too — white text on the solid brand colours:

| Button | Contrast |
|---|---:|
| `primary` — white on `--c-accent` (dark) | **3.64** ✗ |
| `primary` — white on `--c-accent` (light) | **4.42** ✗ |
| `success` — white on `--c-good` | **3.35** ✗ |

That is the main call-to-action in the product ("Sign in", "Save", "Add an asset"), so it is
on the sign-in page Lighthouse audits first.

Worth noting precisely, because the code claims otherwise: the palette docstring in
`index.css` says both modes "clear the lightness band, chroma floor, adjacent-pair CVD
separation and normal-vision floor". That claim is about the **eight series colours used as
chart fills**, where the bar is 3:1 and large-area. The **four status colours used as small
text** were never held to 4.5:1, and the two got conflated at the call sites. The fix is to
split the token: keep `--c-good` etc. for fills and badges' borders, and add
`--c-good-text` / `--c-warn-text` / `--c-serious-text` / `--c-critical-text` darkened
(light) and lightened (dark) to clear 4.5:1. One change in `index.css` plus the `tone` map
in `ui.jsx` fixes every call site at once.

Two more, which axe will not catch but a user will:

- **`Modal` does not trap focus** (`ui.jsx:399-417`). It sets initial focus and handles
  Escape, but Tab walks straight out of the dialog into the page behind it, and focus is not
  restored to the trigger on close.
- **`<html lang="en">`** (`index.html:2`) while much of the rendered content is Persian —
  ticker names, company names, nicknames. The audit passes because the attribute exists, but
  a screen reader will read Persian text with an English voice. Consider `lang="fa"` on the
  elements that hold Persian, which the `Holding.label` path already isolates.

### Best Practices — one guaranteed failure

**No Content-Security-Policy.** Lighthouse runs "CSP is effective against XSS" and it fails
without one. Nothing sets a CSP: not `nginx.conf`, not Django (whose `SECURE_*` block has no
CSP setting and would not cover the HTML document anyway).

For this app a strict CSP is unusually achievable — there are no inline scripts, no
third-party script tags, one origin for the API, and the only external connection is Sentry
when `VITE_SENTRY_DSN` is set. A starting point:

```
default-src 'self';
script-src 'self';
style-src 'self' 'unsafe-inline';        # Tailwind v4 injects a style element
img-src 'self' data:;
font-src 'self';
connect-src 'self' https://*.ingest.sentry.io;
frame-ancestors 'none';
base-uri 'self';
form-action 'self';
object-src 'none'
```

Also missing on the document: HSTS, `X-Content-Type-Options`, `Referrer-Policy`,
`Cross-Origin-Opener-Policy`. Django sets the equivalents for `/api/`; nginx sets none for
`/`. Lighthouse checks HSTS and COOP directly.

Everything else here is in decent shape: no console errors on the signed-out path (the
`restoreSession` fix at `App.jsx:32` removed the guaranteed 401), no deprecated APIs in use,
HTTPS terminated at the edge, correct image aspect ratios (there are no raster images).

### SEO — one line

**No `<meta name="description">`** in `index.html`. That is the only blocking SEO audit;
everything else passes (there is a `<title>`, a viewport meta, a `lang` attribute, no
`robots` blocking, links are crawlable, text is legible).

Add a description, and — since this is an authenticated product — decide deliberately what
you want indexed. A `robots.txt` that allows `/`, `/privacy` and `/terms` and disallows the
app routes is honest and still scores 100.

---

## Enforcing it, and where the tests live

The ask was for tests "in the backend". Lighthouse is a browser audit, so it cannot run
there — but the ask is right in substance, and it splits cleanly into three layers that each
belong somewhere real:

**1. Lighthouse CI in the existing frontend job.** `.github/workflows/ci-cd.yml` already has
a `frontend` job that runs `npm ci`, `npm run test:unit`, `npm run build`, and `deploy`
already depends on it — so a failed assertion blocks the deploy with no wiring change.

```yaml
# in the existing `frontend` job, after `npm run build`
- run: npx serve -s dist -l 4173 &
- run: npx @lhci/cli@0.14.x autorun
```

`lighthouserc.json` asserting all four categories at 1.0, on both targets:

```json
{
  "ci": {
    "collect": {
      "url": ["http://localhost:4173/", "http://localhost:4173/privacy"],
      "numberOfRuns": 3,
      "settings": { "preset": "desktop" }
    },
    "assert": {
      "assertions": {
        "categories:performance":    ["error", { "minScore": 1 }],
        "categories:accessibility":  ["error", { "minScore": 1 }],
        "categories:best-practices": ["error", { "minScore": 1 }],
        "categories:seo":            ["error", { "minScore": 1 }]
      }
    }
  }
}
```

Note the static-server caveat: `npx serve` is not `nginx`, so this run **cannot** verify
compression, cache headers or CSP. Those are config, and config is verified in layer 3.

**2. Authenticated audits in Playwright.** The Dashboard is the page that matters and the
one that can regress. `playwright.config.js` and `e2e/helpers.js` already do the sign-in
flow; add a spec that reuses it and runs Lighthouse against the authenticated page via
`playwright-lighthouse`. Gate the Dashboard on the same four 100s. This is also where a
**per-route bundle-size budget** belongs — assert the `charts-*.js` chunk stays under its
target so echarts cannot silently grow back.

**3. Header and compression assertions — this is the part that genuinely belongs server-side.**
`scripts/deploy.sh` already runs post-deploy verification against live health endpoints and
fails the deploy on a bad check. Extend it to assert on the **document** response:

```bash
h=$(curl -sI -H 'Accept-Encoding: gzip' "https://$HOST/")
grep -qi 'content-security-policy'    <<<"$h" || { echo "no CSP"; exit 1; }
grep -qi 'strict-transport-security'  <<<"$h" || { echo "no HSTS"; exit 1; }
grep -qi 'x-content-type-options'     <<<"$h" || { echo "no nosniff"; exit 1; }
curl -sI -H 'Accept-Encoding: gzip' "https://$HOST/assets/$(…).js" \
  | grep -qi 'content-encoding: *gzip' || { echo "assets uncompressed"; exit 1; }
```

And a genuine pytest for the half Django *does* control — that the production settings block
at `config/settings.py:443-458` cannot be silently weakened:

```python
# backend/tests/test_security_headers.py
def test_production_settings_enforce_transport_security(settings): ...
def test_api_response_sets_nosniff_and_referrer_policy(client): ...
```

---

## What was done

| # | Fix | Category | Where | Status |
|---|---|---|---|---|
| 1 | `gzip on` + `gzip_static` + `gzip_types` | Performance | `frontend/nginx.conf` | **done** |
| 2 | `<meta name="description">`, longer `<title>`, `theme-color` | SEO | `frontend/index.html` | **done** |
| 3 | CSP, HSTS, nosniff, Referrer-Policy, COOP, X-Frame-Options | Best Practices | `frontend/security-headers.conf` | **done** |
| 4 | Status tokens split into fill vs text variants | Accessibility | `index.css`, `ui.jsx`, 27 call sites | **done** |
| 5 | ~~Tree-shake echarts~~ — already done before this work | Performance | `charts.jsx:2-22` | n/a |
| 6 | Lighthouse CI gating the `frontend` job | enforcement | `ci-cd.yml`, `scripts/lhci.sh` | **done** |
| 7 | Focus trap + focus restore in `Modal` | Accessibility | `ui.jsx` | **done** |
| 8 | Authenticated Dashboard audit + chart-chunk budget | enforcement | Playwright | **open** |
| 9 | `robots.txt` | SEO | `public/robots.txt` | **done** |
| 9b | `lang="fa"` on Persian-bearing elements | A11y | `Holding.label` call sites | **open** |
| 10 | Deploy-time header assertions | enforcement | `scripts/deploy.sh` | **open** |

Two fixes were not on the original list and turned out to matter:

**The `add_header` inheritance trap.** nginx does not merge `add_header` across blocks — a
`location` that declares any header of its own silently discards every header inherited from
the server block. `location /assets/` sets `Cache-Control`, so a server-level CSP would have
covered the HTML and left **every JavaScript file with no CSP at all**. That is why the
headers live in `security-headers.conf` and are re-included per location, and why
`scripts/lhci.sh` asserts CSP on a hashed asset and not only on the document.

**Two guaranteed 401s on every anonymous page load.** `restoreSession()` fired
`/api/auth/csrf/` and then `/api/token/refresh/` before knowing whether a session existed;
both answered 401 and the browser logged each as a console error, holding Best Practices at
0.96. `App.jsx:32` had already fixed one instance of exactly this class ("a second red line
in the console of every signed-out visitor") — these were the remaining two. `api.js` now
keeps a `lattice_session` hint, set wherever `auth.tokens` is assigned (login *and* refresh
converge there) and cleared on logout, so a first-time visitor makes **zero** failing
requests. This is a real fix for real users, not an audit workaround.

## How it is enforced

Three layers, because they fail differently.

**1 — `npm run test:lighthouse` → `scripts/lhci.sh`, wired into the existing `frontend` CI
job.** `deploy` already depends on that job, so a regression blocks the deploy with no new
wiring. The script builds `Dockerfile.prod`, runs it behind a small API stub, asserts the
response headers, then runs Lighthouse three times.

Auditing the **real image** rather than `staticDistDir` is the load-bearing choice. Half of
what a 100 depends on is response headers, and a static file server has none of them —
auditing `dist/` would have meant switching off the very audits this work was done to pass,
and would have produced a green build that lies.

The API stub is the other half of that honesty. Without a backend, nginx proxies `/api` to
nothing, the 502 is logged as a console error, and Best Practices drops to 0.96 for a reason
that does not exist in production. The stub answers **401**, not 200 — that is the truthful
response to a session-restore call with no cookie, and it is the path the app already
handles. Faking a signed-in user would audit a page the visitor never sees.

**2 — `src/contrast.test.mjs`, in `npm run test:unit`.** Reads the tokens straight out of
`index.css` and asserts every text-on-surface pair clears 4.5:1 in both modes, and that
white clears 4.5:1 on both solid fills. Deterministic, ~60 ms, no browser. Lighthouse would
catch a contrast regression too, but only on pages it happens to load; this fails on the
commit that introduces it.

It also guards the chart palette — and here it deliberately asserts something weaker.
`index.css` documents that three light-mode series colours sit under 3:1 against white,
because the palette was solved for colour-vision separation first and those charts ship
direct labels alongside. Writing the test made that visible: `--c-s3`, `--c-s4` and `--c-s5`
are exactly the three. Forcing all eight to 3:1 would have meant repaletting to satisfy a
test rather than a user, so the assertion is **"no more than the documented three"** — which
catches the next edit that quietly makes it four, without overriding a decision that was
made deliberately and written down.

**3 — deploy-time header assertions (open, item 10).** `scripts/deploy.sh` already fails a
deploy on a bad post-deploy check. The same `assert_header` calls belong there, run against
the live host, because CI proves the image is right and only production proves the edge
proxy did not strip anything on the way through.

## Holding it

The four scores are green and gated. What keeps them green:

- **The gate is on the deploy path**, not advisory. `frontend` → `deploy` already exists.
- **Performance asserts 0.99, the other three assert 1.0.** Performance is a
  simulated-throttling score and is not stable to the point; a run can land 99 for reasons
  unrelated to the commit. The other three are deterministic, so a failure in them is always
  real. Asserting 1.0 on Performance would produce occasional red builds on unchanged code,
  and a red build people learn to ignore is worse than no gate — which is the argument
  `ci-cd.yml`'s own header comment already makes.
- **Bundle budgets** (`resource-summary:script:size` at 340 KB, stylesheet at 45 KB) catch
  growth before it shows up as a score drop, and specifically stop echarts creeping back.
- **The authenticated audit is measured separately below.** The CI score still describes
  the sign-in page only; authenticated routes require a live backend and credentials, so
  their variable runtime measurement is deliberately not a deployment gate.

## Authenticated routes (2026-09-01)

The 100/100/100/100 above is the **sign-in page** — the only route reachable
without a backend, and therefore the only one CI can gate. It also loads almost
nothing. `npm run test:lighthouse:auth` signs in with a real browser and audits
what is actually behind the JWT:

| Route | Performance | Accessibility | Best practices | SEO | CLS |
|---|:---:|:---:|:---:|:---:|:---:|
| `/` (dashboard) | 99 | **100** | **100** | **100** | 0.062 |
| `/ledger` | **100** | **100** | **100** | 63 ✱ | 0.05 |
| `/optimal` | 95 | **100** | **100** | 63 ✱ | 0.05 |

✱ **SEO 63 is the correct result, not a defect.** Authenticated routes are
`Disallow`ed in robots.txt and the category drops on the single audit "Page is
blocked from indexing". A crawler gets the sign-in shell; there is nothing to
index. Scoring 100 there would mean inviting crawlers into the app, so SEO is
asserted on the public page only.

### What the first authenticated run found

**Two contrast failures, one root cause: `opacity` used for de-emphasis.**
Opacity composites text *towards* the background, so dimming a container drags
every label inside it down with it. The hidden-holdings row at `opacity-50` took
its own "Not counted" badge to 3.05:1 — dimming the badge that explains why the
row is dimmed — and disabled controls at `opacity-40`/`opacity-35` sat at 4.44:1
against a required 4.5. The floor now lives in one exported constant instead of
three numbers chosen independently.

**CLS 0.133 in a single shift.** The charts grid rendered ~84px of "Loading…"
then jumped to 464px when data arrived, shoving the cards below it down the
page. Async panels now reserve the height they are about to occupy — layout
shift is geometry, so a nicer spinner is not a fix.

**Two of the routes did not exist.** There is no `/dashboard` and no
`/my-optimal`; the `*` catch-all served the app, so the scores looked plausible
while measuring URLs the router never defined. robots.txt had drifted the same
way and was missing `/best-overall` and `/breakdown`.

**Charts initialised on mount, including below the fold** — 100ms of blocking
time on `/optimal` spent rendering canvases nobody could see. They now init on
approach, which took that route 91 → 95 and TBT 100ms → 30ms.

### Reading these numbers honestly

Run-to-run variance on this harness is roughly **±4 points**, and it tracks load
on the machine running Chrome, not the app: one run mid-backfill reported the
dashboard at 72 with TBT 1,850ms, while a direct measurement of the same page
showed **zero** long tasks and a 25KB largest payload. A clean re-run returned
99/30ms. Treat a single sub-100 performance score as noise unless a direct
long-task measurement agrees with it — and do not chase the last point or two,
because on this harness that point is not real.

This is not wired into CI: it needs credentials and a live backend, and a gate
that depends on either goes red for reasons that are not the commit's fault.

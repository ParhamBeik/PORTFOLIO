# Test and command evidence

| Check | Result |
| --- | --- |
| Focused member-admin tests, including forced concurrent requests | `9 passed` |
| Full backend PostgreSQL suite | `1154 passed, 1 skipped` in 110.14s; four upstream optimizer deprecation warnings |
| Focused deposit/replay tests | `2 passed` |
| Backend Ruff | passed |
| Frontend targeted route tests | `3 passed` |
| Frontend lint | passed |
| Frontend full unit/Vitest/build before the final route-test addition | `26` standalone tests and `47` Vitest tests passed; build passed |
| Development and production Compose syntax | passed using inert placeholder values for required production-only environment fields |
| Diff integrity | `git diff --check` passed |
| Current focused sell-all API checks | `4 passed` |
| Current sell-all component test | passed inside `5` Dashboard/page component tests |
| Current local frontend suite | `26` standalone tests, `50` Vitest tests, lint, and build passed |
| CI run `34740102439` | terminal-successful: `1155 passed, 3 skipped, 4 warnings`; restore, audits, Lighthouse, headers, and VPS deploy passed |
| Post-deploy SSH/live probes | reviewed SHA, healthy relevant containers, no recent error-level logs, health/readiness/prices and headers passed |

The frontend build retains its existing non-failing charts-bundle size warning. No published
performance budget makes that warning a release failure.

The local Docker daemon was unavailable, so the local production-image Lighthouse command could
not connect. Its mandatory hosted CI equivalent completed successfully in `34740102439`.

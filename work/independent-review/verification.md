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

The frontend build retains its existing non-failing charts-bundle size warning. No published
performance budget makes that warning a release failure.

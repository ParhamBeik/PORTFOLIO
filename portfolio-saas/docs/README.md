# docs/

What each file is, and whether it is still live. Read this before trusting a
date inside one of them.

| File | Kind | Status |
|---|---|---|
| [`REFERENCE.md`](REFERENCE.md) | Standing reference | **Live.** Price-unit policy, warehouse safeguards, recovered-holdings provenance. The one file here with no expiry date. |
| [`DATA-SOURCES.md`](DATA-SOURCES.md) | Standing reference | **Live.** Which origin serves which lane, and which origins the VPS can reach (Iranian VPS since 2026-10; Frankfurt measurements kept as history). |
| [`MOBILE.md`](MOBILE.md) | Release guide | **Live (on `main`).** Capacitor iOS/Android shell, mobile auth, offline snapshot, build and verification. |
| [`STORAGE-POLICY.md`](STORAGE-POLICY.md) | Standing policy, 2026-10-02 | **Live.** Read before changing anything that stores data; linked from `AGENTS.md` and `CLAUDE.md`. |
| [`QUOTA.md`](QUOTA.md) | Standing reference | **Live.** BrsApi wallets, which endpoint bills which plan, measured baselines. |
| [`CODAL-DIRECT-MIGRATION.md`](CODAL-DIRECT-MIGRATION.md) | Migration plan, 2026-10-05 | **Live plan.** BrsApi → codal.ir; phase 1 (shadow discovery, off by default) shipped in #52. |
| [`PERFORMANCE.md`](PERFORMANCE.md) | Standing reference | **Live.** How request/page latency is measured (`perf` app, `manage.py perf_report`), the 2026-10-05 fixes, and what is still open. |
| [`LIGHTHOUSE.md`](LIGHTHOUSE.md) | Audit + enforcement | **Live from "Holding it" onward.** The thresholds it explains are the ones `frontend/lighthouserc.json` asserts today; the audit narrative above that is history. |
| [`PRODUCTION-READINESS.md`](PRODUCTION-READINESS.md) | Working checklist, 2026-09-08/10 | **Mixed.** "P1 — standing" and the SMTP P0 are open; everything under "fixed in batch 5" has shipped. |
| [`PRODUCT-EVALUATION.md`](PRODUCT-EVALUATION.md) | Scored audit, 2026-09-02 | **Roadmap live, scores stale.** The A–F workstream table and the numbered backlog are the current product plan; the Q1–Q7 scores describe the tree at that date. |
| [`MVP-IMPLEMENTATION-CHECKLIST.md`](MVP-IMPLEMENTATION-CHECKLIST.md) | Working checklist, 2026-09-24 | **Snapshot.** Status as of its date; verify against the code before trusting a checkbox. |
| [`MVP-DATA-PREFLIGHT-2026-09-22.md`](MVP-DATA-PREFLIGHT-2026-09-22.md), [`MVP-STAGING-REHEARSAL-2026-09-23.md`](MVP-STAGING-REHEARSAL-2026-09-23.md), [`BRSAPI-QUOTA-EVIDENCE-2026-09-22.md`](BRSAPI-QUOTA-EVIDENCE-2026-09-22.md) | Point-in-time evidence | **History.** Evidence behind the MVP checklist; not current state. |
| [`broker-handoff.md`](broker-handoff.md), [`codal-history-backfill.md`](codal-history-backfill.md), [`balance-sheet-source.md`](balance-sheet-source.md), [`research-budget.md`](research-budget.md), [`research-coverage.md`](research-coverage.md) | Short feature notes | **Live.** Several are linked from code, tests or `deploy.sh`; keep paths stable. |
| [`audit/`](audit/) | Point-in-time audits, 2026-09-25/27 | **History.** Warehouse baseline, production schema dump, FX data quality, Codal page/template inventories, product scope. |
| [`history/`](history/) | Finished work | **History.** `REFACTOR_PLAN.md` / `REFACTOR_REPORT.md` (20-commit refactor, done 2026-09-15). |

Point-in-time operational snapshots are not kept here — the Ops console and the
workflow ledger are the live answer. `QUOTA-USAGE-REPORT-2026-09-04.md` was
removed once its recommendations 1–4 shipped (`exc.reason` branching,
`PACING_REASONS`, jittered rollover wakeups, per-run `rows_created`); 5 and 6 are
subscription-sizing decisions, not code.

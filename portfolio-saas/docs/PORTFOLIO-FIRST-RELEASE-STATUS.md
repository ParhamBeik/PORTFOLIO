# Portfolio-first release status — 2026-09-29

This branch implements a reviewable portfolio and research journey. It is not a production release. Existing accounts, holdings, and ledger rows are preserved.

## Implemented in this branch

- Signed-out visitors land on public research. Signed-in users start at All portfolios, with guided setup for empty accounts. Compare is a top-level destination and keeps the existing comparison modes; contribution replay is its default.
- Portfolio value offers 7, 30, 90, and 365-day and full-history ranges, dated trades, a separate cash-flow-adjusted return index, portfolio summaries, allocation, P&L with recorded income and asset-linked fees, and risk diagnostics with coverage and exclusions.
- Current holdings and the aggregate report price source, age, and quality. Current USD conversion requires a unit-verified, live quote. Historical USD chart and performance conversion require accepted, dated, explicitly Toman-denominated provider FX; absent rates produce gaps.
- Historical snapshot rows are checked against later ledger edits. Recent affected rows are replayed from ledger positions and price history; unreconstructable or missing daily closes remain gaps. Return replay and risk metrics are withheld when required asset prices are missing.
- The stock workspace lists the eligible catalog with explicit financial and total-return gaps. Approved public dossiers show reconciled sales, income, and balance facts with filing links, period, scope, audit state, and million-Rial units. Public price and FX fields remain withheld.

## Gates still closed

1. **Production history rebuild:** Recheck backup, paused queues, storage, live pricing, TSE quantity and Rial/Toman boundaries, then compare known portfolio results on an isolated restore. The read path only replays a bounded recent window and has not rebuilt durable multi-year snapshots.
2. **Public dossiers:** `PUBLIC_DOSSIERS_ENABLED` defaults off and `PUBLIC_DOSSIER_SYMBOLS` is empty. Approve each symbol's source and display rights before enabling it. Third-party public price and FX redistribution remains withheld.
3. **Industry research:** The archived filing inventory is too recent and sparse to certify five years for every issuer. Issuer identity, revision-complete interim quarters, period-average cash USD, six-month whole-industry rankings, and dividend/corporate-action total returns remain unimplemented or uncertified. Their outputs remain gaps.
4. **Paid AI and billing:** `PAID_AI_ENABLED` is closed. A prepaid wallet, verified-email payment flow, ZarinPal merchant approval, funded OpenRouter route, provider usage reconciliation, refunds, bilingual citation/abstention tests, and the 15-second benchmark remain required before activation.

The existing warehouse and recovery evidence is in [warehouse-baseline-2026-09-25.md](audit/warehouse-baseline-2026-09-25.md), [codal-template-inventory-2026-09-26.md](audit/codal-template-inventory-2026-09-26.md), and [broker-handoff.md](broker-handoff.md). This file describes code and release state; it does not claim that production checks or external approvals have passed.

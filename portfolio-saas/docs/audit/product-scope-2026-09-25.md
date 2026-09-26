# Product behavior and pruning decisions — 2026-09-25

This translates the user's research goal into a product contract. It does not inherit requirements from `financial_agent_warehouse_plan.md`: that file is an architectural proposal, and several of its single-schema and USD-precalculation assumptions need source validation first. The [warehouse baseline](warehouse-baseline-2026-09-25.md) records measured production data and recovery status.

## What a user should do each day

1. Open **Portfolio** to see holdings, total/net worth, allocation, and performance in an explicitly selected valuation basis. Open **Activity** to enter and inspect the transaction ledger. These workflows and their history are protected during redesign.
2. Open **Explore** to find a company, inspect its identity, observed share prices, dated disclosure coverage, and certified financial figures. A number displays its original source link, archived artifact hash, source cell, period, unit, scope, and audit status. Corrections can withhold earlier values. Observed balance-sheet templates are shown as point-in-time amounts, separate from income flows.
3. Ask a specific question. Deterministic retrieval and calculations produce eligible observations; a bounded model call chooses and explains among them. A missing unit, unsupported template, incomplete peer set, disputed price, missing FX day, or uncovered period yields a visible gap instead of a plausible estimate. The user chooses the per-run model-cost ceiling; a server ceiling remains in force.
4. For a recurring question, save an explicit metric definition and alert only after its source and comparison rules are stable. Ad hoc curiosity should not require a new static dashboard feature.
5. An operator checks ingestion, off-host backup receipts, queue health, and withheld-fact counts separately from a user's investment view. A data-quality failure should not be hidden inside a nominally successful job.

## Current route decision

| Route or surface | Decision | Evidence and next gate |
| --- | --- | --- |
| `/`, Portfolio summary and breakdown | Preserve, simplify within the page | Protected holdings, net worth, allocation, and performance live here. Price and currency boundaries must remain explicit. Holdings and performance tables now stack labeled rows on phones, preserving the desktop table. |
| `/activity` | Preserve, improve phone layout | Ledger and transaction correction history are core record-keeping. A 390 px live-production check found the desktop-width history table horizontally scrollable, with transaction values and actions off-screen. The draft stacks the same table cells and actions into labeled mobile rows while keeping the desktop table. |
| `/explore` | Grow as primary research path | Source-backed TSE company view and bounded question routing exist in the draft. The observed V9 balance-sheet sheets now have an exact-cell parser and bounded backfill command, but production has not run that backfill. A current catalog industry group fills missing detailed metadata with its source and observation date; it is not a historical peer set. Explore does not yet cover crypto research, cross-company screens, or USD comparisons. |
| `/markets` | Keep reachable during migration | It holds price history and asset comparisons, including non-stock data that Explore does not replace. Move validated workflows into asset-specific exploration before removing its navigation. |
| `/guidance` and portfolio risk panels | Removed from primary navigation and Portfolio in the draft; old `/guidance` links remain reachable during transition | The user finds these of little value; earlier product scoring is not evidence that their underlying forecasts or optimization assumptions are correct. Audit formulas and inputs before retaining any decision claim. |
| `/ops` | Preserve for operators, outside the research flow | Recovery, pipeline, and source coverage checks are necessary even if users rarely visit this page. |

Old deep links still redirect to their current destinations. Deleting a route before its underlying data path and saved links have a replacement would remove access, so pruning is staged by workflow rather than by file count.

## Answer eligibility

| Question family | Current answer | Required before expanding |
| --- | --- | --- |
| A company's current-month sales | Supported only for source-reconciled filings | Broaden template coverage and measure universe completeness; later unverified corrections withhold older values. |
| A company's revenue, net profit, and net margin | Supported only for the two observed V9 income-sheet variants and their exact issuer/period/scope | Backfill stored documents, measure coverage, add other templates and audited variants. Do not mix cumulative interim figures with discrete quarters. |
| A company's assets, liabilities, equity, cash, and borrowings | Supported in the draft only for the two observed V9 balance-sheet variants after their separate source sheets are archived and asset/liability/equity totals reconcile | Run bounded balance-sheet backfill, measure actual universe coverage, then add other observed templates. These are point-in-time Rial facts; do not call all liabilities “debt” or apply an unverified USD rate. |
| Which industry a TSE stock is in now | Current provider-reported sector label with observation time and source | Resolve spelling and issuer changes before peer grouping; collect time-varying membership before a historical comparison. ETFs must not enter a company peer cohort. |
| “Most profitable company in six months” | Withhold | Define profit versus margin versus growth; certify comparable six-month reports across an explicitly selected stock universe, issuer identities, revisions, and industry exclusions. Count and show missing companies. |
| Five-year growth in USD or stock-price return | Withhold | Decide cash-USD/USDT benchmark, daily date alignment, corporate actions and dividends, closing versus transaction/period FX rule, missing-day policy, and whether comparing business results or investor returns. |
| Crypto, gold, and other assets | Portfolio tracking and existing price history remain | Separate quote-asset, venue, unit, adjustment, and liquidity contracts. Do not map them into company balance-sheet fields. |
| Parent, child, and holding-company look-through | Withhold | Time-varying legal-entity graph, ownership percentages, investment disclosures, consolidation elimination rules, and source links. A symbol's subsidiary filing cannot automatically become a parent-company fact. |

The low-cost router is deliberately narrower than a general agent. A later agent may choose among more deterministic tools, but it must keep the same evidence and cost constraints. Expanding tool coverage is the path to better answers; adding unconstrained browsing or more model calls is not a substitute for audited source data.

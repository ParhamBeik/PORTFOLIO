---
name: new-chart
description: Add a new chart or dashboard panel to the portfolio-saas frontend following its existing conventions (shared primitives, themed echarts wrappers, testid naming, useApi fetch pattern). Use when the user asks to add a chart, graph, panel, or new page section to any page under frontend/src/pages/.
---

# Adding a chart or panel to portfolio-saas frontend

This frontend is small and convention-heavy (8 pages, 2 shared component files). Reuse before you build — a new one-off chart or panel is almost always wrong here.

## Before writing anything

1. Check `frontend/src/components/charts.jsx` for an existing wrapper that fits (`AreaTrend`, `MultiLineTrend`, `StackedShareTrend`, `Donut`, `GroupedBar`, `CountTrend`, `MoneyVsRisk`). Reuse it — don't `import echarts` in a page file.
2. Check `frontend/src/components/ui.jsx` for the surface (`Card`, `StatTile`, `Table`, `Disclosure`) — pages never write bespoke panel/loading/error markup.

## If a genuinely new chart type is needed

In `charts.jsx`:
- Add its echarts module to the `use([...])` call at the top — a chart type not registered there renders **silently blank**, no error.
- Never write a hex color. Pull from `SERIES`, `STATUS_COLOR`, `COVERAGE_COLORS`, or `var(--c-*)` tokens (defined in `index.css`), resolved through `useChartTokens()`. That hook re-reads tokens on OS color-scheme change — required for dark/light parity.
- Follow the existing wrapper shape: accepts data + `useChartTokens()` output, returns a component that owns its own `init`/`dispose` lifecycle (see any existing wrapper for the `useRef` + `useEffect` pattern).

## Wiring data

- Fetch through `useApi(fn, deps, opts)` from `useApi.js` — never hand-roll a fetch effect. `pollMs` for live data (dashboard valuation polls every 60s), `enabled` to gate on a selection.
- Render the result through `<Async {...state}>{(data) => ...}</Async>` from `ui.jsx` — handles loading/error/empty so the page component doesn't.
- Add the endpoint call to `api.js` next to its siblings (see `listAssets`, `valuation` for the shape) if it doesn't exist yet.

## Testids

Every interactive or test-relevant element gets `testId` → renders as `data-testid="<page>-<thing>"`, lowercase-hyphenated (e.g. `dashboard-total`, `optimal-window-tabs`). Playwright e2e specs under `frontend/e2e/` select on these — check the relevant `*.spec.js` before naming a new one, to match what tests expect.

## Formatting

Use `format.js` helpers (`toman`, `tomanCompact`, `pct`, `date`, `dateTime`, `humanize`, `assetLabel`) — never format a number or date inline in a page or chart component.

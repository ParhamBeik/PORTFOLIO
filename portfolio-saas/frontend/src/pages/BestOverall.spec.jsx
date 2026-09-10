// Integration: exercise selection, the real API wrapper, and displayed weights
// together at the component boundary, below a full browser e2e test.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import BestOverall from "./BestOverall.jsx";
import { pct } from "../format.js";

const scope = vi.hoisted(() => ({ activeId: 1, basis: "nominal_toman" }));
vi.mock("../components/PortfolioContext.jsx", () => ({ usePortfolio: () => scope }));
vi.mock("../components/charts.jsx", () => ({
  Donut: () => null,
  GroupedBar: ({ data }) => <output data-testid="gap-data">{JSON.stringify(data)}</output>,
}));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps trades and displayed liquid weights scoped when portfolios change", async () => {
  const fetch = vi.fn(async (url) => {
    const account = new URL(url, "http://localhost").searchParams.get("account");
    let data;
    if (url.includes("best-overall")) {
      data = { as_of: "2026-09-08T12:00:00Z", windows: [{
        label: "1Y", max_sharpe: {
          target_weights: { gold: 0.5, usd: 0.5 }, target_metrics: {},
          rebalance_trades: [{ key: "gold", action: account === "1" ? "sell" : "buy",
            delta_weight_pct: account === "1" ? -50 : 50, delta_value_tomans: "50" }],
        },
      }] };
    } else if (url.includes("valuation")) {
      data = { total: "1000", items: [
        { key: "house", class: "Real Estate", value: "900" },
        { key: account === "1" ? "gold" : "usd", class: "Gold", value: "100" },
        { key: "unpriced", class: "Crypto", value: null },
      ] };
    } else {
      data = [];
    }
    return { ok: true, status: 200, json: async () => data };
  });
  vi.stubGlobal("fetch", fetch);
  const view = render(<MemoryRouter><BestOverall /></MemoryRouter>);
  const firstTable = await screen.findByTestId("universe-gap-table");
  expect(within(firstTable).getByText("SELL")).toBeInTheDocument();
  expect(within(firstTable).getByText(pct(1))).toBeInTheDocument();
  expect(JSON.parse(screen.getByTestId("gap-data").textContent)).toEqual([
    { name: "gold", a: 1, b: 0.5 }, { name: "usd", a: 0, b: 0.5 },
  ]);
  expect(fetch.mock.calls.some(([url]) => url === "/api/optimization/best-overall/?account=1")).toBe(true);

  scope.activeId = 2;
  view.rerender(<MemoryRouter><BestOverall /></MemoryRouter>);
  await waitFor(() => expect(within(screen.getByTestId("universe-gap-table")).getByText("BUY")).toBeInTheDocument());
  expect(fetch.mock.calls.some(([url]) => url === "/api/optimization/best-overall/?account=2")).toBe(true);
  expect(JSON.parse(screen.getByTestId("gap-data").textContent)).toEqual([
    { name: "usd", a: 1, b: 0.5 }, { name: "gold", a: 0, b: 0.5 },
  ]);
});

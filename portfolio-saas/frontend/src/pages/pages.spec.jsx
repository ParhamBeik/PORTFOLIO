// Integration test: tests React component composition, API response mapping, and UI element rendering
// for the primary Dashboard and MyOptimal pages against static fixture payloads at the component-boundary tier of the testing pyramid.

import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import Dashboard from "./Dashboard.jsx";
import Comparison from "./Comparison.jsx";
import MyOptimal from "./MyOptimal.jsx";
import { QuotaWallets } from "./Ops.jsx";

// Mock API endpoints called by Dashboard and MyOptimal
vi.mock("../api.js", () => ({
  valuation: vi.fn(),
  snapshots: vi.fn(),
  getPerformance: vi.fn(),
  accountDataQuality: vi.fn(),
  listLiabilities: vi.fn(),
  listAccounts: vi.fn(),
  myOptimal: vi.fn(),
  frontier: vi.fn(),
  listAssets: vi.fn(),
  robustness: vi.fn(),
  analytics: vi.fn(),
  diversifiers: vi.fn(),
  benchmarks: vi.fn(),
  insights: vi.fn(),
  adminAssetEvidence: vi.fn(),
  updateHolding: vi.fn(),
  removeHolding: vi.fn(),
  comparison: vi.fn(),
  corporateActionSuggestions: vi.fn(() => Promise.resolve({ results: [] })),
  acceptCorporateAction: vi.fn(),
  dismissCorporateAction: vi.fn(),
}));

// Mock charts component to avoid canvas context dependencies in jsdom
vi.mock("../components/charts.jsx", () => ({
  AreaTrend: () => <div data-testid="chart-area-trend" />,
  CorrelationHeatmap: () => <div data-testid="chart-correlation-heatmap" />,
  DiversifierScatter: () => <div data-testid="chart-diversifier-scatter" />,
  Donut: () => <div data-testid="chart-donut" />,
  MoneyVsRisk: () => <div data-testid="chart-money-vs-risk" />,
  MultiLineTrend: () => <div data-testid="chart-multi-line-trend" />,
  DriftBars: () => <div data-testid="chart-drift-bars" />,
  GroupedBar: () => <div data-testid="chart-grouped-bar" />,
  RiskScatter: () => <div data-testid="chart-risk-scatter" />,
  STATUS_COLOR: { good: "#3fb950", warn: "#d29922", critical: "#f85149" },
  useChartTokens: () => ({
    text: "#ffffff",
    muted: "#8b949e",
    surface: "#161b22",
    series: ["#58a6ff", "#3fb950"],
  }),
}));

// Mock PortfolioContext
vi.mock("../components/PortfolioContext.jsx", () => ({
  usePortfolio: () => ({
    activeAccount: { id: 1, name: "Main Portfolio" },
    activeId: 1,
    accounts: [{
      id: 1,
      name: "Main Portfolio",
      holdings: [{ id: 42, asset_key: "kama_stock", quantity: "1000" }],
    }],
    basis: "nominal_toman",
    setBasis: vi.fn(),
    setActive: vi.fn(),
    reload: vi.fn(),
  }),
}));

import * as api from "../api.js";

describe("Page Rendering Tests", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders Dashboard with net worth and asset breakdown", async () => {
    api.valuation.mockResolvedValue({
      total: 500000000,
      total_toman: 500000000,
      delta_24h_toman: 1500000,
      delta_24h_pct: 0.003,
      items: [
        {
          key: "kama_stock",
          name: "Kama Stock",
          symbol: "کاما",
          asset_class: "stock",
          quantity: "1000",
          unit_price: 3000,
          unit_price_currency: "rial",
          value_toman: 300000,
          weight: 0.6,
        },
      ],
      quality_status: "complete",
    });

    api.snapshots.mockResolvedValue({
      series: [
        { date: "2026-09-01", total: 498000000 },
        { date: "2026-09-02", total: 500000000 },
      ],
    });

    api.getPerformance.mockResolvedValue({
      twr: 0.15,
      xirr: 0.18,
      is_estimated: false,
    });
    api.listLiabilities.mockResolvedValue([]);
    api.accountDataQuality.mockResolvedValue({
      quality_status: "complete",
      passing_assets: 1,
      assessed_assets: 1,
      assets: [],
    });
    api.analytics.mockResolvedValue({
      sharpe: 1.45,
      volatility: 0.12,
      max_drawdown: -0.08,
      var_95: -0.02,
      cvar_95: -0.03,
      sortino: 1.8,
    });
    api.diversifiers.mockResolvedValue([]);
    api.benchmarks.mockResolvedValue({ benchmarks: [] });
    api.insights.mockResolvedValue({
      concentration: { severity: "ok", message: "No single holding dominates." },
      gold_band: { severity: "ok", message: "Gold allocation is within target band." },
      net_worth_trend: { severity: "info", message: "Not enough history yet." },
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>
    );

    // Verify page header or title is rendered
    await waitFor(() => {
      expect(screen.getByText(/500,000,000 T/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Kama Stock/i)).toBeInTheDocument();
    const holdings = await screen.findByTestId("dashboard-holdings");
    expect(screen.getByTestId("dashboard-trend")).toBeInTheDocument();
    expect(screen.getByTestId("dashboard-allocation")).toBeInTheDocument();
    expect(screen.getByTestId("dashboard-performance")).toBeInTheDocument();
    // Holdings, then returns, then debts. History quality has its own tab.
    const performance = screen.getByTestId("dashboard-performance");
    const liabilities = await screen.findByTestId("dashboard-liabilities");
    expect(
      holdings.compareDocumentPosition(performance) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(
      performance.compareDocumentPosition(liabilities) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(screen.queryByTestId("dashboard-quality")).toBeNull();
    expect(screen.queryByTestId("dashboard-risk")).toBeNull();
    expect(screen.queryByTestId("dashboard-insights")).toBeNull();
    expect(api.analytics).not.toHaveBeenCalled();
    expect(api.diversifiers).not.toHaveBeenCalled();
    expect(api.insights).not.toHaveBeenCalled();
  });

  it("asks before selling all of an edited holding", async () => {
    api.valuation.mockResolvedValue({
      total: 500000000,
      items: [{
        key: "kama_stock",
        name: "Kama Stock",
        quantity: "1000",
        quantity_step: "1",
        unit_price: 3000,
        value: 300000,
        quality_status: "complete",
      }],
    });
    api.snapshots.mockResolvedValue({ series: [] });
    api.getPerformance.mockResolvedValue({ performance_available: false });
    api.accountDataQuality.mockResolvedValue({ quality_status: "complete", assets: [] });
    api.analytics.mockResolvedValue({});
    api.diversifiers.mockResolvedValue([]);
    api.benchmarks.mockResolvedValue({ benchmarks: [] });
    api.insights.mockResolvedValue({});
    api.updateHolding.mockResolvedValue({});
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);

    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    fireEvent.click(await screen.findByTestId("dashboard-holdings-manage-edit"));
    fireEvent.change(screen.getByTestId("dashboard-holdings-edit-qty"), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByTestId("dashboard-holdings-save"));

    expect(confirm).toHaveBeenCalledWith(
      "Sell all of \u2068Kama Stock\u2069? This records a sale in your ledger and removes it from holdings."
    );
    expect(api.updateHolding).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("dashboard-holdings-save"));
    await waitFor(() => expect(api.updateHolding).toHaveBeenCalledWith(1, 42, {
      quantity: "0", confirmSellAll: true, areaSqm: undefined, unitPriceTomans: undefined,
    }));
  });

  it("builds the table one target at a time and follows adds and removals", async () => {
    const series = [
      { label: "Mine", unit: "toman", points: [{ date: "2026-09-01", value: 10 }] },
      { label: "x", unit: "toman", points: [{ date: "2026-09-01", value: 20 }] },
    ];
    const subject = {
      key: "mine", label: "Mine", units: 27000000,
      invested_tomans: 4761305759, value_tomans: 17469000000,
      price: { value: 6470, unit: "IRR" }, priced_live: true, end_date: "2026-10-12",
    };
    const ROWS = {
      gold: {
        key: "gold", label: "Gold", units: 1218.4,
        value_tomans: 31631961015, actual_tomans: 17469000000,
        invested_tomans: 4761305759, difference_tomans: -14162961015,
        difference_pct: -44.77, breakeven_price: { value: 11716, unit: "IRR" },
        start_date: "2022-01-05", end_date: "2026-10-12", window_days: 1743,
        series, warnings: [],
      },
      coin: { key: "coin", label: "Coin", reason: "missing_price_history", detail: "No usable price history for Coin." },
      euro: {
        key: "euro", label: "Euro", units: 0.25,
        value_tomans: 900000000, actual_tomans: 800000000,
        invested_tomans: 700000000, difference_tomans: -100000000,
        difference_pct: -11.1, breakeven_price: { value: 333, unit: "IRR" },
        start_date: "2022-01-05", end_date: "2026-09-20", window_days: 1720,
        series, warnings: [],
      },
    };
    api.comparison.mockImplementation((_accountId, options) => {
      if (!options) {
        return Promise.resolve({
          holdings: [
            { key: "unpriced", label: "Unpriced", priced: false },
            { key: "mine", label: "Mine", priced: true },
          ],
          targets: [
            { key: "gold", label: "Gold" },
            { key: "coin", label: "Coin" },
            { key: "euro", label: "Euro" },
          ],
          default_targets: ["gold", "coin", "euro"],
          omitted_holdings: [],
        });
      }
      return Promise.resolve({ subject, rows: [ROWS[options.targets[0]]], requested_days: null });
    });

    render(
      <MemoryRouter>
        <Comparison />
      </MemoryRouter>
    );

    const table = await screen.findByTestId("comparison-table");
    await waitFor(() => expect(table).toHaveTextContent("0.25"));
    // Opens on the holding that has money in it, one request per target.
    const asked = api.comparison.mock.calls.filter(([, o]) => o).map(([, o]) => o);
    expect(asked.every((o) => o.subject === "mine" && o.targets.length === 1)).toBe(true);
    expect(asked.map((o) => o.targets[0])).toEqual(["gold", "coin", "euro"]);

    expect(table).toHaveTextContent("14,162,961,015 T");
    expect(table).not.toHaveTextContent("-14,162,961,015 T");
    expect(table).toHaveTextContent("-44.8%");
    expect(table).toHaveTextContent("11,716 ﷼");
    expect(table).toHaveTextContent("No usable price history for Coin.");
    expect(screen.getByTestId("comparison-price")).toHaveTextContent("6,470 ﷼");
    // The euro's series stopped early: its value is dated, not called today's.
    expect(screen.getAllByTestId("comparison-row-as-of")).toHaveLength(1);

    // Removing is immediate and fetches nothing; putting it back is cached.
    const before = api.comparison.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /Remove Gold/ }));
    expect(screen.getByTestId("comparison-table")).not.toHaveTextContent("31,631,961,015 T");
    expect(screen.getByTestId("comparison-table")).toHaveTextContent("Euro");
    expect(api.comparison.mock.calls.length).toBe(before);
  });

  it("keeps the table when one target is rate-limited, and retries just that row", async () => {
    let busy = true;
    api.comparison.mockImplementation((_accountId, options) => {
      if (!options) {
        return Promise.resolve({
          holdings: [{ key: "mine", label: "Mine", priced: true }],
          targets: [{ key: "gold", label: "Gold" }, { key: "coin", label: "Coin" }],
          default_targets: ["gold", "coin"],
          omitted_holdings: [],
        });
      }
      const key = options.targets[0];
      if (key === "coin" && busy) {
        busy = false;
        return Promise.reject(Object.assign(new Error("Too many requests."), { status: 429 }));
      }
      return Promise.resolve({
        subject: { key: "mine", label: "Mine", units: 10, invested_tomans: 100, value_tomans: 200, price: { value: 200, unit: "IRT" }, end_date: "2026-10-12" },
        rows: [{ key, label: key === "gold" ? "Gold" : "Coin", units: 1, value_tomans: 300, actual_tomans: 200, invested_tomans: 100, difference_tomans: -100, difference_pct: -33.3, breakeven_price: { value: 300, unit: "IRT" }, start_date: "2026-01-01", end_date: "2026-10-12", window_days: 280, series: [], warnings: [] }],
      });
    });

    render(
      <MemoryRouter>
        <Comparison />
      </MemoryRouter>
    );

    const table = await screen.findByTestId("comparison-table");
    await waitFor(() => expect(table).toHaveTextContent("Too many requests."));
    expect(table).toHaveTextContent("Gold");
    fireEvent.click(screen.getByTestId("comparison-row-retry"));
    await waitFor(() => expect(screen.getByTestId("comparison-table")).not.toHaveTextContent("Too many requests."));
  });

  it("renders MyOptimal with scenarios and recommended weights", async () => {
    api.myOptimal.mockResolvedValue({
      as_of: "2026-09-08T12:00:00Z",
      windows: [
        {
          label: "1Y",
          min_volatility: {
            weights: { kama_stock: 0.5, usd_cash: 0.5 },
            metrics: {
              volatility: 0.08,
              cvar_95: -0.02,
              sharpe: 1.6,
              expected_return: 0.2,
            },
          },
          max_sharpe: {
            weights: { kama_stock: 0.8, usd_cash: 0.2 },
            metrics: {
              volatility: 0.14,
              cvar_95: -0.04,
              sharpe: 2.1,
              expected_return: 0.32,
            },
          },
          current: {
            weights: { kama_stock: 1.0 },
            metrics: {
              volatility: 0.22,
              cvar_95: -0.07,
              sharpe: 1.1,
              expected_return: 0.24,
            },
          },
        },
      ],
      asset_labels: {
        kama_stock: "Kama Stock",
        usd_cash: "US Dollar",
      },
      warnings: [],
    });

    api.frontier.mockResolvedValue({
      points: [
        { volatility: 0.08, expected_return: 0.2 },
        { volatility: 0.14, expected_return: 0.32 },
      ],
    });
    api.listAssets.mockResolvedValue([]);
    api.robustness.mockResolvedValue({ stability_score: 0.85 });

    render(
      <MemoryRouter>
        <MyOptimal />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("optimal-scenario-tabs-min_volatility")
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("optimal-scenario-tabs-max_sharpe")
    ).toBeInTheDocument();
    expect(screen.getByTestId("optimal-as-of")).toBeInTheDocument();
  });

  it("renders an unmetered provider product without a false zero ceiling", () => {
    render(
      <QuotaWallets
        quota={{
          plans: {
            market_cgcc: {
              plan: "market_cgcc",
              metered: false,
              used: 475,
              archive_used: 200,
              live_used: 250,
              other_used: 25,
              unattributed: 0,
            },
          },
        }}
      />
    );

    expect(screen.getByText("Unmetered")).toBeInTheDocument();
    expect(screen.getByText("provider-billed requests")).toBeInTheDocument();
    expect(screen.getByText(/Provider counter not observed/)).toBeInTheDocument();
    expect(screen.queryByText(/\/ 0 provider-billed requests/)).not.toBeInTheDocument();
  });
});

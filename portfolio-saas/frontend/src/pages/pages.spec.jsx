// Integration test: tests React component composition, API response mapping, and UI element rendering
// for the primary Dashboard and MyOptimal pages against static fixture payloads at the component-boundary tier of the testing pyramid.

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import Dashboard from "./Dashboard.jsx";
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
    accounts: [{ id: 1, name: "Main Portfolio" }],
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
    const quality = await screen.findByTestId("dashboard-quality");
    const notes = await screen.findByTestId("dashboard-insights");
    const liabilities = await screen.findByTestId("dashboard-liabilities");
    expect(
      holdings.compareDocumentPosition(quality) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(
      quality.compareDocumentPosition(notes) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(
      notes.compareDocumentPosition(liabilities) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
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
            brs: {
              plan: "brs",
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
    expect(screen.getByText("requests today")).toBeInTheDocument();
    expect(screen.queryByText(/\/ 0 today/)).not.toBeInTheDocument();
  });
});

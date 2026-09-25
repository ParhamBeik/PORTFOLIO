import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import Explore from "./Explore.jsx";
import * as api from "../api.js";

vi.mock("../api.js", () => ({
  exploreStocks: vi.fn(),
  stockDossier: vi.fn(),
  researchSettings: vi.fn(),
  runResearch: vi.fn(),
}));

vi.mock("../components/charts.jsx", () => ({
  MultiLineTrend: () => <div data-testid="mock-trend" />,
}));

const source = {
  date: "2026-06-21", period_start_jalali: "1405-03-01",
  period_end_jalali: "1405-03-31", value: "545287525",
  source_url: "https://www.codal.ir/Reports/Decision.aspx?LetterSerial=1",
  extraction_id: 2, artifact_id: 3041, report_id: 1001,
  source_coordinates: { css: "table:nth-of-type(1)", row: 30, column: 17 },
};

describe("company research", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.exploreStocks.mockResolvedValue([{ symbol: "فولاد", name: "Foolad" }]);
    api.stockDossier.mockResolvedValue({
      company: { name: "Foolad", symbol: "فولاد", sector: "Steel" },
      price: { points: [], paired_candle_days: 0, candle_disagreements_over_1pct: 0, source: "TSETMC" },
      monthly_sales: { points: [source], verified_periods: 1, withheld_periods: 0, latest_filing_periods: 1 },
      financial_metrics: {
        points: [{
          period_start_jalali: "1405-01-01", period_end_jalali: "1405-03-31",
          scope: "standalone", audited: false, revenue: "834166799",
          net_profit: "120356493", net_margin_pct: "14.43",
          artifact_id: 14169, published_jalali: "1405-05-01",
          source_url: source.source_url,
          source_coordinates: { revenue: { address: "B4" }, net_profit: { address: "B21" } },
        }], verified_periods: 1, withheld_periods: 0,
      },
      coverage: [], disclosures: [],
    });
    api.researchSettings.mockResolvedValue({
      provider_status: "ready", provider_model: "gemini-2.5-flash-lite",
      max_run_usd: "0.10", default_run_usd: "0.01",
    });
  });

  it("shows the selected cost ceiling and source-backed answer", async () => {
    api.runResearch.mockResolvedValue({
      status: "answered", cost_usd: "0.000400", cost_basis: "provider_reported",
      claims: [{ id: "highest", statement: "Highest among verified months: 1405-03-31 at 545,287,525 million Rial.", sources: [source] }],
      coverage: { verified_periods: 1, withheld_periods: 0 },
    });
    render(<MemoryRouter initialEntries={["/explore?symbol=فولاد"]}><Explore /></MemoryRouter>);

    const question = await screen.findByTestId("explore-research-question");
    const budget = screen.getByTestId("explore-research-budget");
    expect(budget).toHaveValue(0.01);
    fireEvent.change(question, { target: { value: "Which month had the highest sales?" } });
    fireEvent.change(budget, { target: { value: "0.02" } });
    fireEvent.click(screen.getByTestId("explore-research-submit"));

    await waitFor(() => expect(api.runResearch).toHaveBeenCalledWith("فولاد", "Which month had the highest sales?", "0.02"));
    expect(await screen.findByText(/Highest among verified months/)).toBeInTheDocument();
    expect(screen.getByText(/Model cost: \$0\.0004/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /Codal filing/i })[0]).toHaveAttribute("href", source.source_url);
    expect(screen.getByTestId("explore-income-evidence")).toHaveTextContent(/revenue cell B4 · profit cell B21/i);
  });
});

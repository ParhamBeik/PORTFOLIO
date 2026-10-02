import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DataHealth from "./DataHealth.jsx";
import * as api from "../api.js";

vi.mock("../api.js", () => ({ valuation: vi.fn(), accountDataQuality: vi.fn() }));
vi.mock("../components/PortfolioContext.jsx", () => ({
  usePortfolio: () => ({ activeId: 7, accounts: [{ id: 7, name: "Family" }] }),
}));

describe("data health", () => {
  it("shows each price's source and status, and what the backfill is doing", async () => {
    api.valuation.mockResolvedValue({
      items: [{ key: "kama", label: "کاما", source: "API", priced_at: "2026-10-01T10:00:00Z", age_seconds: 300, quality_status: "stale" }],
    });
    api.accountDataQuality.mockResolvedValue({
      assets: [{ asset_key: "kama", symbol: "کاما", passes_gate: false, reason_codes: ["low_coverage"], repair_state: "failed", next_repair_at: null }],
    });

    render(<DataHealth />);

    expect(await screen.findByText("API")).toBeInTheDocument();
    expect(screen.getByText("Stale")).toBeInTheDocument();
    expect(await screen.findByText("Retrying after failures")).toBeInTheDocument();
    expect(screen.getByText("Price history · Family")).toBeInTheDocument();
  });
});

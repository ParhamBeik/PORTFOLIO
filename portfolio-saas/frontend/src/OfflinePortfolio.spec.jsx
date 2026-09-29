import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const unlockOfflineSnapshot = vi.fn();
const addListener = vi.fn(async () => ({ remove: vi.fn() }));

vi.mock("./mobile.js", () => ({
  unlockOfflineSnapshot: (...args) => unlockOfflineSnapshot(...args),
}));

vi.mock("@capacitor/app", () => ({
  App: {
    addListener: (...args) => addListener(...args),
  },
}));

const { default: OfflinePortfolio, holdingRowKey } = await import("./OfflinePortfolio.jsx");

afterEach(() => {
  cleanup();
  unlockOfflineSnapshot.mockReset();
  addListener.mockClear();
});

describe("holdingRowKey", () => {
  it("prefers the stored snapshot key", () => {
    expect(holdingRowKey({ key: "TSE:ABC", asset_key: "other" }, 3)).toBe("TSE:ABC");
  });

  it("falls back through identity fields then index", () => {
    expect(holdingRowKey({ account_id: 2, label: "Gold" }, 0)).toBe("2:Gold");
    expect(holdingRowKey({}, 4)).toBe("all:4");
  });
});

describe("OfflinePortfolio", () => {
  beforeEach(() => {
    unlockOfflineSnapshot.mockResolvedValue({
      accounts: [{ id: 1, name: "Family" }],
      scopes: {
        "all:nominal_toman": {
          updatedAt: "2026-09-24T12:00:00.000Z",
          valuation: {
            total: "100",
            basis: "nominal_toman",
            items: [
              { key: "TSE:ABC", label: "سهم", class: "Stocks", quantity: "1", quantity_step: "1", value: "100" },
            ],
          },
          history: { series: [{ date: "2026-09-23", total: "90" }, { date: "2026-09-24", total: "100" }] },
        },
      },
    });
  });

  it("unlocks the saved snapshot and renders holdings keyed by item.key", async () => {
    render(<OfflinePortfolio available onReconnect={vi.fn()} onSignOut={vi.fn()} />);
    fireEvent.click(screen.getByTestId("offline-unlock"));
    await waitFor(() => expect(screen.getByTestId("offline-total")).toBeInTheDocument());
    expect(screen.getByText("سهم")).toBeInTheDocument();
    expect(unlockOfflineSnapshot).toHaveBeenCalledOnce();
  });

  it("locks again when the document becomes hidden", async () => {
    render(<OfflinePortfolio available onReconnect={vi.fn()} onSignOut={vi.fn()} />);
    fireEvent.click(screen.getByTestId("offline-unlock"));
    await screen.findByTestId("offline-total");
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(screen.queryByTestId("offline-total")).toBeNull());
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });
});

/**
 * The first-run page: "Step 1 of 3" shows only on a first run, and a member
 * who owns a home and nothing on the exchange can start with the home.
 */
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  addHolding: vi.fn(),
  addProperty: vi.fn(),
  createAccount: vi.fn(),
  listAssets: vi.fn(),
}));
vi.mock("../api.js", () => api);

const portfolio = vi.hoisted(() => ({ value: null }));
vi.mock("../components/PortfolioContext.jsx", () => ({
  usePortfolio: () => portfolio.value,
}));

const { default: Onboarding } = await import("./Onboarding.jsx");

const mount = (accounts) => {
  portfolio.value = { accounts, activeId: null, setActive: vi.fn(), reload: vi.fn() };
  api.listAssets.mockResolvedValue([{ key: "gold:coin", name: "Coin" }]);
  return render(<MemoryRouter><Onboarding /></MemoryRouter>);
};

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("Onboarding", () => {
  it("starts a brand-new member on a house without asking for a quantity", async () => {
    api.createAccount.mockResolvedValue({ id: 7 });
    api.addProperty.mockResolvedValue({});
    mount([]);
    expect(screen.getByTestId("onboarding-steps")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("onboarding-kind"), { target: { value: "house" } });
    expect(screen.queryByTestId("onboarding-quantity")).toBeNull();
    fireEvent.change(screen.getByTestId("onboarding-house-name"), { target: { value: "Home" } });
    fireEvent.change(screen.getByTestId("onboarding-house-area"), { target: { value: "80" } });
    fireEvent.change(screen.getByTestId("onboarding-house-price"), { target: { value: "95" } });
    fireEvent.click(screen.getByTestId("onboarding-submit"));
    await waitFor(() => expect(api.addProperty).toHaveBeenCalledWith(7, {
      name: "Home", areaSqm: "80", pricePerSqmMillion: "95",
    }));
    expect(api.addHolding).not.toHaveBeenCalled();
  });

  it("hides the step bar and the kind switch once something is held", () => {
    mount([{ id: 1, name: "Family", holdings: [{ asset_key: "gold:coin" }] }]);
    expect(screen.queryByTestId("onboarding-steps")).toBeNull();
    expect(screen.queryByTestId("onboarding-kind")).toBeNull();
  });
});

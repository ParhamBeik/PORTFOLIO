/**
 * The liability flow, mounted for real.
 *
 * This exists because of a bug that reached production: `LiabilityDialog`
 * rendered a `<Select>` that was never imported, so pressing "Add liability"
 * threw a ReferenceError and took the dashboard down with it. Vite's build does
 * not resolve identifiers, there is no ESLint in this project, and no e2e spec
 * opened the dialog -- so nothing anywhere caught it. Worse, the crash only
 * fired on the "All portfolios" branch, which is where the portfolio picker
 * appears, so the half of the flow anyone tested by hand worked fine.
 *
 * The assertions are therefore deliberately about MOUNTING, and about both
 * branches of that condition. Anything that renders is enough to prove the
 * component's identifiers all resolve.
 */
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api.js", () => ({
  listLiabilities: vi.fn(async () => []),
  createLiability: vi.fn(async () => ({})),
  updateLiability: vi.fn(async () => ({})),
  deleteLiability: vi.fn(async () => ({})),
}));

const { default: LiabilitiesCard, LiabilityDialog, LiabilityRow } = await import(
  "./Liabilities.jsx"
);

const ACCOUNTS = [
  {
    id: 1,
    name: "Family",
    // Shaped as `HoldingSerializer` actually serializes: `label` is the
    // holder's own name for the asset, falling back to the catalog's.
    holdings: [
      {
        asset_key: "house_tehran",
        display_name: "Tehran flat",
        label: "Tehran flat",
        asset_name: "Real Estate",
      },
      {
        asset_key: "emami_coin",
        label: "سکه امامی",
        asset_name: "Emami Coin",
        asset_name_fa: "سکه امامی",
      },
    ],
  },
  { id: 2, name: "Trading", holdings: [] },
];

afterEach(cleanup);

describe("LiabilityDialog", () => {
  it("mounts with a portfolio picker when no portfolio is selected", () => {
    render(
      <LiabilityDialog accountId={null} accounts={ACCOUNTS} onClose={() => {}} />
    );
    // The branch that used to throw: `accountId` falsy is what renders the
    // picker, and the picker is what referenced the missing import.
    expect(screen.getByTestId("liability-account")).toBeInTheDocument();
    expect(screen.getByTestId("liability-dialog")).toBeInTheDocument();
  });

  it("mounts without the picker when a portfolio is already in scope", () => {
    render(<LiabilityDialog accountId={1} accounts={ACCOUNTS} onClose={() => {}} />);
    expect(screen.queryByTestId("liability-account")).toBeNull();
    expect(screen.getByTestId("liability-label")).toBeInTheDocument();
  });

  it("offers the assets a debt can be secured against", () => {
    render(<LiabilityDialog accountId={1} accounts={ACCOUNTS} onClose={() => {}} />);
    const picker = screen.getByTestId("liability-asset");
    expect(picker).toBeInTheDocument();
    // Named the way the holder named it, not "Real Estate" -- a property is
    // minted per owner and the catalog row has only the asset class.
    expect(picker.textContent).toContain("Tehran flat");
  });

  it("asks for loan terms and refuses a half-filled schedule", () => {
    render(<LiabilityDialog accountId={1} accounts={ACCOUNTS} onClose={() => {}} />);
    fireEvent.click(screen.getByTestId("liability-mode-rate"));

    fireEvent.change(screen.getByTestId("liability-label"), {
      target: { value: "Mortgage" },
    });
    fireEvent.change(screen.getByTestId("liability-principal"), {
      target: { value: "600000000" },
    });
    fireEvent.change(screen.getByTestId("liability-rate"), {
      target: { value: "20" },
    });

    // No term and no start date yet: saving is blocked, and the reason is on
    // screen rather than hidden behind a disabled button.
    expect(screen.getByTestId("liability-save")).toBeDisabled();
    expect(screen.getByTestId("liability-incomplete").textContent).toMatch(
      /installments/i
    );
  });

  it("names the asset a secured debt must be attached to", () => {
    render(<LiabilityDialog accountId={1} accounts={ACCOUNTS} onClose={() => {}} />);
    fireEvent.click(screen.getByTestId("liability-kind-secured_debt"));
    fireEvent.change(screen.getByTestId("liability-label"), {
      target: { value: "Mortgage" },
    });

    expect(screen.getByTestId("liability-incomplete").textContent).toMatch(
      /secured against/i
    );
  });
});

describe("LiabilityRow", () => {
  it("shows a bank loan's schedule and what is still owed", () => {
    render(
      <LiabilityRow
        row={{
          id: 1,
          label: "Mortgage",
          kind: "bank_loan",
          lender: "Bank Maskan",
          asset_label: "Tehran flat",
          amount_tomans: "600000000",
          outstanding_tomans: "382416958",
          principal_tomans: "600000000",
          balance_basis: "amortized",
          installments_paid: 29,
          term_months: 60,
          scheduled_installment_tomans: "15895000",
          payoff_on: "2029-01-15",
        }}
        onEdit={() => {}}
        onDelete={() => {}}
      />
    );

    expect(screen.getByText(/Bank loan/)).toBeInTheDocument();
    expect(screen.getByText(/on Tehran flat/)).toBeInTheDocument();
    expect(screen.getByText(/29 of 60 installments paid/)).toBeInTheDocument();
    // The derived balance, not the stored column beside it.
    expect(screen.getByText(/382,416,958 T/)).toBeInTheDocument();
    expect(screen.getByText("still owed")).toBeInTheDocument();
  });

  it("says a declared balance is only what was entered", () => {
    render(
      <LiabilityRow
        row={{
          id: 2,
          label: "Owed to a friend",
          kind: "other",
          amount_tomans: "50000000",
          outstanding_tomans: "50000000",
          balance_basis: "declared",
        }}
        onEdit={() => {}}
        onDelete={() => {}}
      />
    );

    expect(screen.getByText("as entered")).toBeInTheDocument();
    // No schedule, so no invented progress: "0 of 0 installments" is a claim
    // about a loan that has no installments.
    expect(screen.queryByText(/installments paid/)).toBeNull();
  });
});

describe("LiabilitiesCard", () => {
  it("mounts and renders its add button", async () => {
    render(<LiabilitiesCard activeId={1} accounts={ACCOUNTS} />);
    expect(await screen.findByTestId("liability-add")).toBeInTheDocument();
  });

  // The crash that reached production only fired when no portfolio was selected,
  // because that is the branch that mounts the portfolio picker.
  it("opens the add dialog on the all-portfolios branch", async () => {
    render(<LiabilitiesCard activeId={null} accounts={ACCOUNTS} />);
    fireEvent.click(await screen.findByTestId("liability-add"));
    expect(screen.getByTestId("liability-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("liability-account")).toBeInTheDocument();
  });
});

import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { Table } from "./ui.jsx";

it("keeps every value and row action in a labeled mobile transaction card", () => {
  render(
    <Table
      caption="Transaction history"
      mobileCards
      rows={[{ id: 1, asset: "فولاد", value: "1,000 T" }]}
      rowKey={(row) => row.id}
      columns={[
        { key: "asset", header: "Asset" },
        { key: "value", header: "Value" },
        { key: "actions", header: "", render: () => <button>Edit</button> },
      ]}
    />
  );

  const table = screen.getByRole("table", { name: "Transaction history" });
  const row = within(table).getAllByRole("row")[1];
  expect(within(row).getByText("Asset")).toBeInTheDocument();
  expect(within(row).getByText("فولاد")).toBeInTheDocument();
  expect(within(row).getByText("Value")).toBeInTheDocument();
  expect(within(row).getByText("1,000 T")).toBeInTheDocument();
  expect(within(row).getByRole("button", { name: "Edit" })).toBeInTheDocument();
});

import { describe, expect, it } from "vitest";
import { hasAnyHoldings } from "./holdingsGate.js";

describe("hasAnyHoldings", () => {
  it("is false when every account is empty", () => {
    expect(hasAnyHoldings([{ holdings: [] }, { holdings: [] }])).toBe(false);
  });

  it("is true when any account has a holding", () => {
    expect(hasAnyHoldings([{ holdings: [] }, { holdings: [{ id: 1 }] }])).toBe(true);
  });
});

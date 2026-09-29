import { describe, expect, it } from "vitest";
import { mergeHistory } from "./Family.jsx";

describe("portfolio history gaps", () => {
  it("does not turn missing closes into zero or carry an earlier value forward", () => {
    const history = mergeHistory([
      { id: 1, points: [
        { date: "2026-09-01", total: "100" },
        { date: "2026-09-02", total: null },
        { date: "2026-09-03", total: "120" },
      ] },
      { id: 2, points: [
        { date: "2026-09-01", total: "100" },
        { date: "2026-09-02", total: "100" },
      ] },
    ]);
    expect(history.values.map((row) => row._total)).toEqual([200, null, null]);
    expect(history.values[1]["1"]).toBeNull();
    expect(history.values[2]["2"]).toBeNull();
    expect(history.shares[0]["1"]).toBe(0.5);
    expect(history.shares[1]["1"]).toBeNull();
    expect(history.shares[2]["2"]).toBeNull();
  });
});

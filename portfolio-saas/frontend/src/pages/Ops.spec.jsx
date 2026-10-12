import { describe, expect, it } from "vitest";
import { dailyAdds } from "./Ops.jsx";

describe("dailyAdds", () => {
  it("turns 8-hourly totals into rows added per day, keeping unmeasured keys out", () => {
    const history = [
      { captured_at: "2026-10-10T06:00:00Z", counts: { ticks: 100 } },
      { captured_at: "2026-10-10T22:00:00Z", counts: { ticks: 150 } },
      { captured_at: "2026-10-11T06:00:00Z", counts: { ticks: null } },
      { captured_at: "2026-10-11T22:00:00Z", counts: { ticks: 400 } },
      { captured_at: "2026-10-12T06:00:00Z", counts: { ticks: 390 } },
    ];
    expect(dailyAdds(history, "ticks")).toEqual([
      { x: "2026-10-11", y: 250 },
      { x: "2026-10-12", y: 0 },
    ]);
  });

  it("leaves a day with no sample empty instead of piling its growth onto the next", () => {
    const history = [
      { captured_at: "2026-10-01T22:00:00Z", counts: { ticks: 100 } },
      { captured_at: "2026-10-04T22:00:00Z", counts: { ticks: 400 } },
      { captured_at: "2026-10-05T22:00:00Z", counts: { ticks: 450 } },
    ];
    expect(dailyAdds(history, "ticks")).toEqual([
      { x: "2026-10-02", y: null },
      { x: "2026-10-03", y: null },
      { x: "2026-10-04", y: null },
      { x: "2026-10-05", y: 50 },
    ]);
  });
});

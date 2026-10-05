import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("perf page-ready tracking", () => {
  let perf;
  let sent;

  beforeEach(async () => {
    vi.resetModules();
    vi.useFakeTimers();
    sent = [];
    globalThis.fetch = vi.fn((url, init) => {
      sent.push(...JSON.parse(init.body).events);
      return Promise.resolve({ ok: true });
    });
    Object.defineProperty(navigator, "sendBeacon", { value: undefined, configurable: true });
    perf = await import("./perf.js");
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("waits for a waterfall to finish before reporting the page ready", () => {
    perf.beginPage("/:breakdown");
    const first = perf.trackLoad();
    vi.advanceTimersByTime(100);
    first();
    // A dependent request starts inside the settle window: still the same page load.
    vi.advanceTimersByTime(100);
    const second = perf.trackLoad();
    vi.advanceTimersByTime(400);
    second();
    vi.advanceTimersByTime(400);
    perf.flush();

    const pages = sent.filter((e) => e.kind === "page");
    expect(pages).toHaveLength(1);
    expect(pages[0].route).toBe("/:breakdown");
    expect(pages[0].ms).toBeGreaterThanOrEqual(600);
    expect(sent.filter((e) => e.kind === "boot")).toHaveLength(1);
  });

  it("drops a measurement abandoned by navigating away", () => {
    perf.beginPage("/");
    const stale = perf.trackLoad();
    perf.beginPage("/activity");
    stale();
    vi.advanceTimersByTime(1000);
    perf.flush();
    expect(sent.filter((e) => e.kind === "page")).toHaveLength(0);
  });

  it("records api timings but never its own beacon", () => {
    perf.recordApi("/api/valuation/", "GET", 200, 123.4);
    perf.recordApi("/api/perf/client/", "POST", 202, 5);
    perf.flush();
    expect(sent).toEqual([{ kind: "api", route: "/api/valuation/", method: "GET", status: 200, ms: 123 }]);
  });
});

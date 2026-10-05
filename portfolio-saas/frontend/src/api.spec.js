import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api.js";

function jsonResponse(body) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
    headers: new Headers(),
  });
}

describe("api() in-flight GET sharing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends one request for identical concurrent GETs and gives each caller its own copy", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((url) =>
      String(url).includes("/api/perf/") ? jsonResponse({}) : jsonResponse({ items: [3, 1, 2] })
    );
    const [a, b] = await Promise.all([api("/api/valuation/"), api("/api/valuation/")]);
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/valuation/"));
    expect(calls).toHaveLength(1);
    expect(a).toEqual(b);
    a.items.sort();
    expect(b.items).toEqual([3, 1, 2]);
  });

  it("does not share writes or sequential GETs", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() => jsonResponse({ ok: 1 }));
    await api("/api/accounts/");
    await api("/api/accounts/");
    await Promise.all([
      api("/api/accounts/", { method: "POST", body: { name: "x" } }),
      api("/api/accounts/", { method: "POST", body: { name: "x" } }),
    ]);
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/accounts/"));
    expect(calls).toHaveLength(4);
  });
});

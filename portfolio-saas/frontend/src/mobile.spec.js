import { beforeEach, describe, expect, it, vi } from "vitest";

const entries = new Map();

vi.mock("@capacitor/core", () => ({ Capacitor: { isNativePlatform: () => true } }));
vi.mock("@aparajita/capacitor-secure-storage", () => ({
  SecureStorage: {
    setKeyPrefix: vi.fn(), setSynchronize: vi.fn(),
    get: async (key) => entries.get(key) ?? null,
    set: async (key, value) => { entries.set(key, value); },
    remove: async (key) => { entries.delete(key); },
  },
}));
vi.mock("@aparajita/capacitor-biometric-auth", () => ({
  BiometricAuth: {
    checkBiometry: async () => ({ deviceIsSecure: true }),
    authenticate: vi.fn(),
  },
}));
vi.mock("@capacitor/browser", () => ({ Browser: { open: vi.fn() } }));
vi.mock("@capacitor/filesystem", () => ({ Filesystem: { writeFile: vi.fn(), deleteFile: vi.fn() }, Directory: { Cache: "CACHE" } }));
vi.mock("@capacitor/share", () => ({ Share: { share: vi.fn() } }));

const mobile = await import("./mobile.js");

beforeEach(async () => {
  await mobile.clearMobileData();
  entries.clear();
});

describe("mobile offline snapshot", () => {
  it("keeps only essential figures and preserves the server's money basis", async () => {
    await mobile.prepareMobileAccount(1);
    mobile.captureOfflineResponse("/api/valuation/?basis=usd_denominated", {
      total: "142.50", basis: "nominal_toman", prices: { secret: 99 },
      items: [{ key: "TSE:ABC", label: "سهم", class: "Stocks", quantity: "10", quantity_step: "1", value: "142.50", unit_price: "14.25" }],
    });
    mobile.captureOfflineResponse("/api/snapshots/?days=30&basis=usd_denominated", {
      basis: "nominal_toman", trades: [{ secret: 1 }], series: [{ date: "2026-09-23", total: "142.50", extra: "omit" }],
    });
    expect(await mobile.hasOfflineSnapshot()).toBe(true);
    const snapshot = await mobile.unlockOfflineSnapshot();
    expect(snapshot.scopes["all:usd_denominated"].valuation).toEqual({
      total: "142.50", basis: "nominal_toman",
      items: [{ key: "TSE:ABC", label: "سهم", name_fa: undefined, name: undefined, class: "Stocks", account_id: undefined, quantity: "10", quantity_step: "1", value: "142.50" }],
    });
    expect(snapshot.scopes["all:usd_denominated"].history.series).toEqual([{ date: "2026-09-23", total: "142.50" }]);
  });

  it("erases one account before preparing a different account", async () => {
    await mobile.prepareMobileAccount(1);
    mobile.captureOfflineResponse("/api/valuation/", { total: "123", basis: "nominal_toman", items: [] });
    expect(await mobile.hasOfflineSnapshot()).toBe(true);
    await mobile.prepareMobileAccount(2);
    expect(await mobile.hasOfflineSnapshot()).toBe(false);
    await expect(mobile.unlockOfflineSnapshot()).rejects.toThrow("No saved portfolio");
  });

  it("erases the snapshot and refresh token on sign-out", async () => {
    await mobile.prepareMobileAccount(1);
    await mobile.setRefreshToken("secret");
    mobile.captureOfflineResponse("/api/valuation/", { total: "123", items: [] });
    await mobile.clearMobileData();
    expect(await mobile.getRefreshToken()).toBeNull();
    expect(await mobile.hasOfflineSnapshot()).toBe(false);
  });
});

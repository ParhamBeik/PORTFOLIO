import { Capacitor } from "@capacitor/core";
import { SecureStorage } from "@aparajita/capacitor-secure-storage";
import { BiometricAuth } from "@aparajita/capacitor-biometric-auth";
import { Browser } from "@capacitor/browser";
import { Filesystem, Directory } from "@capacitor/filesystem";
import { Share } from "@capacitor/share";

export const isNative = Capacitor.isNativePlatform();
export const API_ORIGIN = "https://portfolio.parhambm.ir";

let storageReady;
// Capacitor plugin proxies expose a dynamic `then` method. Returning the proxy
// from an async function makes Promise treat it as a thenable and stalls boot.
const secureStore = {
  get: (...args) => SecureStorage.get(...args),
  set: (...args) => SecureStorage.set(...args),
  remove: (...args) => SecureStorage.remove(...args),
};
async function storage() {
  if (!isNative) throw new Error("Device storage is only available in the mobile app.");
  storageReady ||= (async () => {
    await SecureStorage.setKeyPrefix("holdings_v1_");
    await SecureStorage.setSynchronize(false);
  })();
  await storageReady;
  return secureStore;
}

export async function getRefreshToken() {
  return (await (await storage()).get("refresh", false)) || null;
}

export async function setRefreshToken(token) {
  const store = await storage();
  if (token) await store.set("refresh", token);
  else await store.remove("refresh");
}

export async function prepareMobileAccount(userId) {
  const store = await storage();
  await snapshotQueue;
  const previousId = await store.get("user_id", false);
  if (previousId && String(previousId) !== String(userId)) {
    await store.remove(SNAPSHOT_KEY);
    snapshotState = null;
  }
  snapshotState ||= await store.get(SNAPSHOT_KEY, false);
  if (snapshotState?.userId !== Number(userId)) {
    snapshotState = { userId: Number(userId), accounts: [], scopes: {} };
    await store.set(SNAPSHOT_KEY, snapshotState);
  }
  await store.set("user_id", String(userId));
}

const SNAPSHOT_KEY = "portfolio_snapshot";
let snapshotQueue = Promise.resolve();
let snapshotState = null;

function savedValuation(data) {
  return {
    total: data.total,
    basis: data.basis,
    items: (data.items || []).map((item) => ({
      key: item.key,
      label: item.label,
      name_fa: item.name_fa,
      name: item.name,
      class: item.class,
      account_id: item.account_id,
      quantity: item.quantity,
      quantity_step: item.quantity_step,
      value: item.value,
    })),
  };
}

function scopeKey(params) {
  return `${params.get("account") || "all"}:${params.get("basis") || "nominal_toman"}`;
}

export function captureOfflineResponse(path, data) {
  if (!isNative || !data) return;
  const url = new URL(path, API_ORIGIN);
  if (!["/api/auth/me/", "/api/accounts/", "/api/valuation/", "/api/snapshots/"].includes(url.pathname)) return;
  snapshotQueue = snapshotQueue.then(async () => {
    const store = await storage();
    if (url.pathname === "/api/auth/me/") {
      const nextId = data.id;
      if (!nextId) return;
      if (!snapshotState) snapshotState = await store.get(SNAPSHOT_KEY, false);
      if (snapshotState?.userId !== nextId) snapshotState = { userId: nextId, accounts: [], scopes: {} };
      await store.set("user_id", String(nextId));
    } else {
      if (!snapshotState) snapshotState = await store.get(SNAPSHOT_KEY, false);
      if (!snapshotState?.userId) return;
      if (url.pathname === "/api/accounts/") {
        snapshotState.accounts = data.map(({ id, name }) => ({ id, name }));
      } else {
        const key = scopeKey(url.searchParams);
        const scope = snapshotState.scopes[key] || {};
        if (url.pathname === "/api/valuation/") {
          scope.valuation = savedValuation(data);
          scope.updatedAt = new Date().toISOString();
        } else if (url.searchParams.get("days") === "30") {
          scope.history = {
            basis: data.basis,
            series: (data.series || []).map(({ date, total }) => ({ date, total })),
          };
        }
        snapshotState.scopes[key] = scope;
      }
    }
    await store.set(SNAPSHOT_KEY, snapshotState);
  }).catch(() => {});
}

export async function hasOfflineSnapshot() {
  if (!isNative) return false;
  await snapshotQueue;
  const store = await storage();
  const [id, snapshot] = await Promise.all([
    store.get("user_id", false), store.get(SNAPSHOT_KEY, false),
  ]);
  return Boolean(id && snapshot?.userId === Number(id) && Object.values(snapshot.scopes || {}).some((s) => s.valuation));
}

export async function unlockOfflineSnapshot() {
  const available = await BiometricAuth.checkBiometry();
  if (!available.deviceIsSecure) throw new Error("Set a device screen lock to view saved portfolio data.");
  await BiometricAuth.authenticate({
    reason: "Unlock your saved Holdings portfolio",
    allowDeviceCredential: true,
    androidTitle: "Unlock Holdings",
  });
  await snapshotQueue;
  const store = await storage();
  const [id, snapshot] = await Promise.all([
    store.get("user_id", false), store.get(SNAPSHOT_KEY, false),
  ]);
  if (!id || snapshot?.userId !== Number(id) || !Object.values(snapshot.scopes || {}).some((scope) => scope.valuation)) {
    throw new Error("No saved portfolio for this account.");
  }
  return snapshot;
}

export async function clearMobileData() {
  if (!isNative) return;
  await snapshotQueue;
  const store = await storage();
  snapshotState = null;
  await Promise.all([store.remove("refresh"), store.remove("user_id"), store.remove(SNAPSHOT_KEY)]);
}

export async function openExternal(url) {
  if (!isNative) {
    window.open(url, "_blank", "noopener,noreferrer");
    return;
  }
  await Browser.open({ url: new URL(url, API_ORIGIN).href });
}

export async function shareExport(blob) {
  const buffer = await blob.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  const name = `holdings-export-${new Date().toISOString().slice(0, 10)}.zip`;
  const file = await Filesystem.writeFile({
    path: name,
    data: btoa(binary),
    directory: Directory.Cache,
  });
  try {
    await Share.share({ title: "Holdings data export", url: file.uri });
  } finally {
    await Filesystem.deleteFile({ path: name, directory: Directory.Cache });
  }
}

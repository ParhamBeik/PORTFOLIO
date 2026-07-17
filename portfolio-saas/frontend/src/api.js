// Tiny API client: wraps fetch with JWT auth + auto-refresh on expiry.
// Keep it dependency-free; this is the whole networking layer.

const API_BASE = import.meta.env.VITE_API_URL || "";
const ACCESS_KEY = "ps_access";
const REFRESH_KEY = "ps_refresh";

export const auth = {
  get token() {
    return localStorage.getItem(ACCESS_KEY);
  },
  set tokens({ access, refresh }) {
    localStorage.setItem(ACCESS_KEY, access);
    if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
  },
  logout() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

async function refreshAccessToken() {
  const refresh = localStorage.getItem(REFRESH_KEY);
  if (!refresh) return null;
  const res = await fetch(`${API_BASE}/api/token/refresh/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });
  if (!res.ok) return null;
  const data = await res.json();
  localStorage.setItem(ACCESS_KEY, data.access);
  return data.access;
}

export async function api(path, { method = "GET", body, _retried = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`;
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  // On expiry, try one silent refresh then replay the original request.
  if (res.status === 401 && !_retried && auth.token) {
    const fresh = await refreshAccessToken();
    if (fresh) return api(path, { method, body, _retried: true });
    auth.logout();
    throw new Error("Session expired");
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || JSON.stringify(detail) || res.statusText);
  }
  return res.status === 204 ? null : res.json();
}

// Auth
export const register = (email, password) =>
  api("/api/auth/register/", { method: "POST", body: { email, password } });
export const login = (email, password) =>
  api("/api/auth/login/", { method: "POST", body: { email, password } });
export const me = () => api("/api/auth/me/");
// NOTE: tier upgrades go through billing (Part 3-B), not a self-service endpoint.

// Catalog & accounts
export const listAssets = () => api("/api/assets/");
export const listAccounts = () => api("/api/accounts/");
export const createAccount = (name, broker = "") =>
  api("/api/accounts/", { method: "POST", body: { name, broker } });
export const updateAccount = (id, { name, broker }) =>
  api(`/api/accounts/${id}/`, { method: "PATCH", body: { name, broker } });
export const deleteAccount = (id) =>
  api(`/api/accounts/${id}/`, { method: "DELETE" });
export const accountValuation = (id) => api(`/api/accounts/${id}/valuation/`);
export const listHoldings = (accountId) =>
  api(`/api/accounts/${accountId}/holdings/`);
export const addHolding = (accountId, assetKey, quantity) =>
  api(`/api/accounts/${accountId}/holdings/`, {
    method: "POST",
    body: { asset_key: assetKey, quantity: Number(quantity) },
  });
export const updateHolding = (accountId, id, quantity) =>
  api(`/api/accounts/${accountId}/holdings/${id}/`, {
    method: "PATCH",
    body: { quantity: Number(quantity) },
  });
export const removeHolding = (accountId, id) =>
  api(`/api/accounts/${accountId}/holdings/${id}/`, { method: "DELETE" });

// Valuation & pricing
export const valuation = () => api("/api/valuation/");
export const latestPrices = () => api("/api/prices/latest/");
export const priceHistory = (assetKey, limit = 100) =>
  api(`/api/prices/history/?asset=${encodeURIComponent(assetKey)}&limit=${limit}`);
export const insights = () => api("/api/insights/");

// Billing — Stripe Checkout. Returns { url }; the browser redirects there.
export const createCheckout = () =>
  api("/api/billing/checkout/", { method: "POST" });

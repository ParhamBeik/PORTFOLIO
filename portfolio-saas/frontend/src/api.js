// Tiny API client: wraps fetch with JWT auth + auto-refresh on expiry.
// Keep it dependency-free; this is the whole networking layer.

const API_BASE = import.meta.env.VITE_API_URL || "";
export const SESSION_EXPIRED_EVENT = "lattice:session-expired";
let accessToken = null;
let refreshPromise = null;

export const auth = {
  get token() {
    return accessToken;
  },
  set tokens({ access }) {
    accessToken = access || null;
  },
  logout() {
    accessToken = null;
  },
};

const cookie = (name) => document.cookie
  .split("; ")
  .find((part) => part.startsWith(`${name}=`))
  ?.split("=").slice(1).join("=");

async function csrfToken() {
  const existing = cookie("csrftoken");
  if (existing) return decodeURIComponent(existing);
  const response = await fetch(`${API_BASE}/api/auth/csrf/`, { credentials: "include" });
  if (!response.ok) return null;
  return (await response.json()).csrf_token;
}

async function refreshAccessToken() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = csrfToken().then((csrf) => fetch(`${API_BASE}/api/token/refresh/`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRFToken": csrf } : {}) },
    body: "{}",
  }))
    .then(async (res) => {
      if (!res.ok) return null;
      const data = await res.json();
      auth.tokens = data;
      return data.access;
    })
    .finally(() => {
      refreshPromise = null;
    });
  return refreshPromise;
}

export const restoreSession = () => refreshAccessToken();

export async function logoutSession(allDevices = false) {
  try {
    const csrf = await csrfToken();
    await fetch(`${API_BASE}/api/auth/${allDevices ? "logout-all" : "logout"}/`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "X-CSRFToken": csrf } : {}),
        ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
      },
      body: "{}",
    });
  } finally {
    auth.logout();
  }
}

function expireSession() {
  auth.logout();
  window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

function apiError(message, status) {
  const error = new Error(message);
  error.status = status;
  return error;
}

export async function api(path, { method = "GET", body, _retried = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`;
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "include",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  // On expiry, try one silent refresh then replay the original request.
  if (res.status === 401 && !_retried && auth.token) {
    const fresh = await refreshAccessToken();
    if (fresh) return api(path, { method, body, _retried: true });
    expireSession();
    throw apiError("Session expired", 401);
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw apiError(extractError(detail) || res.statusText, res.status);
  }
  return res.status === 204 ? null : res.json();
}

// DRF errors come in several shapes: {"detail": "..."} for auth/permission,
// {"field": ["msg", ...]} for validation, {"non_field_errors": [...]} for
// object-level. Flatten whichever we got into one readable line so the UI never
// shows a bare "{}" or "[object Object]".
function extractError(detail) {
  if (!detail || typeof detail !== "object") return String(detail || "");
  if (typeof detail.detail === "string") return detail.detail;
  const parts = [];
  for (const [field, val] of Object.entries(detail)) {
    const msg = Array.isArray(val) ? val.join(" ") : String(val);
    parts.push(field === "non_field_errors" ? msg : `${field}: ${msg}`);
  }
  return parts.join(" · ");
}

// Auth
export const register = (email, password, firstName = "", lastName = "") =>
  api("/api/auth/register/", {
    method: "POST",
    body: { email, password, first_name: firstName, last_name: lastName },
  });
export const login = (email, password) =>
  api("/api/auth/login/", { method: "POST", body: { email, password } });
export const me = () => api("/api/auth/me/");
export const updateProfile = (data) =>
  api("/api/auth/me/", { method: "PATCH", body: data });
export const changePassword = (oldPassword, newPassword, confirmPassword) =>
  api("/api/auth/change-password/", {
    method: "POST",
    body: {
      old_password: oldPassword,
      new_password: newPassword,
      confirm_password: confirmPassword,
    },
  });
// NOTE: tier upgrades go through billing (Part 3-B), not a self-service endpoint.


// Catalog & accounts
export const listAssets = () => api("/api/assets/");
export const listAccounts = () => api("/api/accounts/");
export const createAccount = (name, broker = "") =>
  api("/api/accounts/", { method: "POST", body: { name, broker } });
export const updateAccount = (id, { name, broker, goal }) =>
  api(`/api/accounts/${id}/`, {
    method: "PATCH",
    body: { name, broker, goal },
  });
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

// Buy/sell: the ledger write path. Appends a Transaction, updates the holding
// balance, and stamps a net-worth snapshot — all atomically on the backend.
export const trade = (accountId, { assetKey, side, quantity, note = "", timestamp = null }) =>
  api(`/api/accounts/${accountId}/trades/`, {
    method: "POST",
    body: { asset_key: assetKey, side, quantity: Number(quantity), note, timestamp },
  });
// Trade history (all accounts, or one via ?account=). Newest first.
export const transactions = (days = 90, accountId = null) =>
  api(`/api/transactions/?days=${days}` + (accountId ? `&account=${accountId}` : ""));
export const deleteTransaction = (id) =>
  api(`/api/transactions/${id}/`, { method: "DELETE" });
export const getPerformance = (accountId, basis = "nominal_toman") =>
  api(`/api/accounts/${accountId}/performance/?basis=${basis}`);
export const getIntegrity = () =>
  api(`/api/integrity/`);

// Valuation & pricing
//
// `account` is the active-portfolio id (null = "All portfolios", the aggregate).
// It threads `?account=` into the per-portfolio endpoints so the whole UI scopes
// to the portfolio selected in the top bar.
const accountParam = (account) => (account ? `account=${account}` : "");
export const valuation = (account = null, basis = null) => {
  let url = `/api/valuation/`;
  const params = [];
  if (account) params.push(`account=${account}`);
  if (basis) params.push(`basis=${basis}`);
  if (params.length > 0) {
    url += "?" + params.join("&");
  }
  return api(url);
};
export const latestPrices = () => api("/api/prices/latest/");
export const priceHistory = (assetKey, limit = 100) =>
  api(`/api/prices/history/?asset=${encodeURIComponent(assetKey)}&limit=${limit}`);
export const insights = (account = null) =>
  api(`/api/insights/${accountParam(account) ? "?" + accountParam(account) : ""}`);

// FREE: net-worth history for the trend chart. account=None -> aggregate series;
// an account id -> that portfolio's per-account snapshot series.
export const snapshots = (days = 30, account = null) =>
  api(
    `/api/snapshots/?days=${days}` + (account ? `&${accountParam(account)}` : "")
  );

// Pro analytics & optimization. All gated by IsPro on the backend. All scope to
// the active portfolio via ?account=.
export const analytics = (account = null) =>
  api(`/api/analytics/${accountParam(account) ? "?" + accountParam(account) : ""}`);
export const optimize = (scenario, constraints = null, account = null) =>
  api(
    `/api/optimization/${accountParam(account) ? "?" + accountParam(account) : ""}`,
    {
      method: "POST",
      body: constraints ? { scenario, constraints } : { scenario },
    }
  );
export const frontier = (account = null) =>
  api(
    `/api/optimization/frontier/${accountParam(account) ? "?" + accountParam(account) : ""}`
  );
export const assetReturns = (days = 180) => api(`/api/assets/returns/?days=${days}`);

// Billing — Zarinpal. Returns { redirect_url }; the browser redirects there.
// After paying, Zarinpal calls our callback, which verifies and bounces the
// browser back to /billing?status=success&ref_id=.. (or cancel/error).
export const createZarinpalPayment = () =>
  api("/api/billing/zarinpal/request/", { method: "POST" });

// Market data (TSE). Symbols/candles/history/index are FREE;
// announcements & shareholders are Pro (403 for free users).
export const marketSymbols = () => api("/api/market/symbols/");
export const marketAssets = () => api("/api/market/assets/");
export const marketPerformance = (asset, limit = 5000) =>
  api(`/api/market/performance/?asset=${encodeURIComponent(asset)}&limit=${limit}`);
export const marketCandles = (symbol, timeframe = "1d_adj", limit = 200) =>
  api(
    `/api/market/candles/?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`
  );
export const marketHistory = (symbol, { adjusted = 1, limit = 365 } = {}) =>
  api(
    `/api/market/history/?symbol=${encodeURIComponent(symbol)}&adjusted=${adjusted}&limit=${limit}`
  );
export const marketTicks = (symbol, date = "", limit = 500) =>
  api(
    `/api/market/ticks/?symbol=${encodeURIComponent(symbol)}${date ? `&date=${encodeURIComponent(date)}` : ""}&limit=${limit}`
  );
export const marketIndex = (limit = 365) => api(`/api/market/index/?limit=${limit}`);
export const marketAnnouncements = (symbol, limit = 20) =>
  api(`/api/market/announcements/?symbol=${encodeURIComponent(symbol)}&limit=${limit}`);
export const marketShareholders = (symbol) =>
  api(`/api/market/shareholders/?symbol=${encodeURIComponent(symbol)}`);
export const adminStatus = () => api("/api/market/admin/status/");
export const adminStatusStreamUrl = () => {
  const token = auth.token;
  return `${API_BASE}/api/market/admin/stream/${token ? `?token=${encodeURIComponent(token)}` : ""}`;
};
export const adminCleanPricesScan = () => api("/api/admin/clean-prices/scan/");
export const adminCleanPricesExecute = (confirm) =>
  api("/api/admin/clean-prices/execute/", { method: "POST", body: { confirm } });

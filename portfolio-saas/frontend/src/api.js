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

let sessionExpiresAt = null;
export const sessionExpiry = {
  get value() {
    return sessionExpiresAt;
  },
  set(iso) {
    sessionExpiresAt = iso || null;
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
      sessionExpiry.set(data.session_expires_at);
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
    sessionExpiry.set(null);
  }
}

function expireSession() {
  auth.logout();
  sessionExpiry.set(null);
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
    // `statusText` is ALWAYS "" over HTTP/2, which is what production serves. So
    // any error the body didn't explain arrived as an Error with an empty
    // message, and the UI fell back to a bare "Something went wrong." with
    // nothing to act on. Say at least what the server said.
    throw apiError(
      extractError(detail) || res.statusText || `Request failed (${res.status}).`,
      res.status
    );
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
export const register = (email, password) =>
  api("/api/auth/register/", { method: "POST", body: { email, password } });
export const login = (email, password) =>
  api("/api/auth/login/", { method: "POST", body: { email, password } });
export const me = () => api("/api/auth/me/");
export const listAssets = () => api("/api/assets/");
export const searchAssetCatalog = (assetClass, q = "") =>
  api(`/api/assets/catalog/${qs({ asset_class: assetClass, q })}`);
export const ensureAsset = (source, symbol) =>
  api("/api/assets/ensure/", { method: "POST", body: { source, symbol } });
export const listAccounts = () => api("/api/accounts/");
export const createAccount = (name, broker = "") =>
  api("/api/accounts/", { method: "POST", body: { name, broker } });
export const addHolding = (accountId, assetKey, quantity) =>
  api(`/api/accounts/${accountId}/holdings/`, {
    method: "POST",
    body: { asset_key: assetKey, quantity: Number(quantity) },
  });

// A property is described by its size and what a square meter is worth, in
// millions of Toman — never by a bare "quantity". `newPropertyName` mints a new
// one; passing `assetKey` instead revalues a property already held.
export const addProperty = (accountId, { name, areaSqm, pricePerSqmMillion, mortgageTomans, occurredAt }) =>
  api(`/api/accounts/${accountId}/holdings/`, {
    method: "POST",
    body: {
      new_property_name: name,
      area_sqm: Number(areaSqm),
      price_per_sqm_million: Number(pricePerSqmMillion),
      ...(mortgageTomans ? { mortgage_deduction_tomans: Number(mortgageTomans) } : {}),
      ...(occurredAt ? { occurred_at: occurredAt } : {}),
    },
  });

const numeric = (key, value) =>
  value == null || value === "" ? {} : { [key]: Number(value) };

export const updateHolding = (
  accountId,
  id,
  { quantity, unitPriceTomans, areaSqm, pricePerSqmMillion, displayName, isHidden, occurredAt } = {}
) =>
  api(`/api/accounts/${accountId}/holdings/${id}/`, {
    method: "PATCH",
    body: {
      ...numeric("quantity", quantity),
      ...numeric("unit_price_tomans", unitPriceTomans),
      ...numeric("area_sqm", areaSqm),
      ...numeric("price_per_sqm_million", pricePerSqmMillion),
      ...(displayName != null ? { display_name: displayName } : {}),
      ...(isHidden != null ? { is_hidden: isHidden } : {}),
      ...(occurredAt ? { occurred_at: occurredAt } : {}),
    },
  });
export const removeHolding = (accountId, id) =>
  api(`/api/accounts/${accountId}/holdings/${id}/`, { method: "DELETE" });

// Buy/sell: the ledger write path. Appends a Transaction, updates the holding
// balance, and stamps a net-worth snapshot — all atomically on the backend.
export const trade = (
  accountId,
  { assetKey, side, quantity, note = "", timestamp = null, priceTomans = null }
) =>
  api(`/api/accounts/${accountId}/trades/`, {
    method: "POST",
    body: {
      asset_key: assetKey,
      side,
      quantity: Number(quantity),
      note,
      ...(timestamp ? { timestamp } : {}),
      // Omitted means "use the market price for that date"; the backend resolves
      // it from the warehouse rather than the client guessing.
      ...numeric("price_tomans", priceTomans),
    },
  });
export const getPerformance = (accountId, basis = "nominal_toman") =>
  api(`/api/accounts/${accountId}/performance/?basis=${basis}`);

// The account ledger: add, edit, and delete trades. Holdings without history
// appear as position rows. accountId null = every portfolio.
export const listLedger = (accountId) =>
  accountId ? api(`/api/accounts/${accountId}/ledger/`) : api("/api/ledger/");
export const createLedgerEntry = (accountId, entry) =>
  api(`/api/accounts/${accountId}/ledger/`, { method: "POST", body: entry });
export const updateLedgerEntry = (accountId, entryId, body) =>
  api(`/api/accounts/${accountId}/ledger/${entryId}/`, { method: "PATCH", body });
export const deleteLedgerEntry = (accountId, entryId) =>
  api(`/api/accounts/${accountId}/ledger/${entryId}/`, { method: "DELETE" });
export const updateLedgerHolding = (accountId, holdingId, quantity) =>
  api(`/api/accounts/${accountId}/ledger/holdings/${holdingId}/`, {
    method: "PATCH",
    body: { quantity: Number(quantity) },
  });
export const deleteLedgerHolding = (accountId, holdingId) =>
  api(`/api/accounts/${accountId}/ledger/holdings/${holdingId}/`, { method: "DELETE" });

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
// Net-worth history for the trend chart. account=None -> aggregate series;
// an account id -> that portfolio's per-account snapshot series.
export const snapshots = (days = 30, account = null, basis = null) => {
  let url = `/api/snapshots/?days=${days}`;
  if (account) url += `&${accountParam(account)}`;
  if (basis) url += `&basis=${basis}`;
  return api(url);
};

// Portfolio against what you could have held instead, indexed to 100.
export const benchmarks = (account = null, { basis, window } = {}) => {
  const params = new URLSearchParams();
  if (account) params.set("account", account);
  if (basis) params.set("basis", basis);
  if (window != null) params.set("window", String(window));
  const qs = params.toString();
  return api(`/api/analytics/benchmarks/${qs ? `?${qs}` : ""}`);
};

// Candidates ranked by what they would do to portfolio RISK, not by past return.
export const diversifiers = (account = null, { basis, window } = {}) => {
  const params = new URLSearchParams();
  if (account) params.set("account", account);
  if (basis) params.set("basis", basis);
  if (window != null) params.set("window", String(window));
  const qs = params.toString();
  return api(`/api/analytics/diversifiers/${qs ? `?${qs}` : ""}`);
};

// Analytics & optimization, scoped to the active portfolio via ?account=.
export const analytics = (account = null, { basis, window } = {}) => {
  const params = new URLSearchParams();
  if (account) params.set("account", account);
  if (basis) params.set("basis", basis);
  if (window != null) params.set("window", String(window));
  const qs = params.toString();
  return api(`/api/analytics/${qs ? `?${qs}` : ""}`);
};
export const frontier = (account = null, { window } = {}) => {
  const params = new URLSearchParams();
  if (account) params.set("account", account);
  if (window != null) params.set("window", String(window));
  const qs = params.toString();
  return api(`/api/optimization/frontier/${qs ? `?${qs}` : ""}`);
};
export const myOptimal = (account = null) =>
  api(
    `/api/optimization/my-optimal/${accountParam(account) ? "?" + accountParam(account) : ""}`
  );
export const bestOverall = () => api("/api/optimization/best-overall/");

function qs(params) {
  const u = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v != null && v !== "") u.set(k, String(v));
  });
  const s = u.toString();
  return s ? `?${s}` : "";
}

export const adminOverview = (params) => api(`/api/admin/overview/${qs(params)}`);
export const adminWorkflows = (params) => api(`/api/admin/workflows/${qs(params)}`);
export const adminArchiveStates = (params) => api(`/api/admin/archive-states/${qs(params)}`);
export const adminArchiveRetry = (ids) =>
  api("/api/admin/archive-states/retry/", { method: "POST", body: { ids, confirm: true } });
export const adminAssets = (params) => api(`/api/admin/assets/${qs(params)}`);
export const adminAssetEvidence = (key) =>
  api(`/api/admin/assets/${encodeURIComponent(key)}/evidence/`);
export const adminAssetRetry = (key) =>
  api(`/api/admin/assets/${encodeURIComponent(key)}/retry/`, { method: "POST", body: { confirm: true } });
export const adminAssetRecomputeIntegrity = (key) =>
  api(`/api/admin/assets/${encodeURIComponent(key)}/recompute-integrity/`, { method: "POST", body: { confirm: true } });
export const adminAssetRefresh = (key) =>
  api(`/api/admin/assets/${encodeURIComponent(key)}/refresh/`, { method: "POST", body: { confirm: true } });


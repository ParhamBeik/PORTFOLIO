// Tiny API client: wraps fetch with JWT auth + auto-refresh on expiry.
// Keep it dependency-free; this is the whole networking layer.

const API_BASE = import.meta.env.VITE_API_URL || "";
export const SESSION_EXPIRED_EVENT = "lattice:session-expired";
let accessToken = null;
let refreshPromise = null;

// Whether this browser has ever held a session, so a first-time visitor does not
// pay for a refresh that can only fail.
//
// The refresh cookie is httpOnly and therefore unreadable, so asking the server
// is the only way to KNOW -- but a signed-out visitor does not need to know. Two
// requests fired on every anonymous page load, both answering 401, and the
// browser logs each one as a console error: `/api/auth/csrf/` then
// `/api/token/refresh/`. That is the same class of defect the `restoreSession`
// comment in App.jsx already fixed once ("a second red line in the console of
// every signed-out visitor") -- these are the remaining two, and they are what
// held Lighthouse's Best Practices at 0.96.
//
// Worst case this hint is missing while the cookie is still valid (the user
// cleared site data), and they sign in again. That is strictly better than two
// guaranteed failures on every first visit.
const SESSION_HINT = "lattice_session";

export function hasSessionHint() {
  try {
    return localStorage.getItem(SESSION_HINT) === "1";
  } catch {
    // Private mode / storage disabled: fall back to asking the server, which is
    // the behaviour this replaced and is still correct, just chattier.
    return true;
  }
}

function setSessionHint(on) {
  try {
    if (on) localStorage.setItem(SESSION_HINT, "1");
    else localStorage.removeItem(SESSION_HINT);
  } catch {
    /* storage unavailable; hasSessionHint() already fails open */
  }
}

export const auth = {
  get token() {
    return accessToken;
  },
  set tokens({ access }) {
    accessToken = access || null;
    // Set on login AND on every successful refresh, which is the one place both
    // paths already converge.
    setSessionHint(!!access);
  },
  logout() {
    accessToken = null;
    setSessionHint(false);
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

// Resolves null without touching the network when this browser has never held a
// session. `App.jsx` already treats null as "anonymous", which is a normal state.
export const restoreSession = () =>
  (hasSessionHint() ? refreshAccessToken() : Promise.resolve(null));

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
  // A FormData body reaches fetch untouched: stringifying it would send the
  // literal "[object FormData]", and declaring Content-Type ourselves would
  // strip the multipart boundary only the browser can generate. Uploads go
  // through here so they keep the 401 refresh-and-replay below.
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  const headers = isForm ? {} : { "Content-Type": "application/json" };
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`;
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "include",
    headers,
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
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
export const updateProfile = ({ firstName, lastName }) =>
  api("/api/auth/me/", {
    method: "PATCH",
    body: { first_name: firstName, last_name: lastName },
  });
export const changePassword = ({ oldPassword, newPassword, confirmPassword }) =>
  api("/api/auth/change-password/", {
    method: "POST",
    body: {
      old_password: oldPassword,
      new_password: newPassword,
      confirm_password: confirmPassword,
    },
  }).then((data) => {
    // The server rotates every token when a password changes, so the access
    // token in hand is dead the moment this returns. Adopting the fresh pair it
    // hands back is what keeps the user signed in instead of bouncing them to
    // the login screen for having successfully changed their password.
    auth.tokens = data;
    return data;
  });
export const deleteAccount = (password) =>
  api("/api/auth/me/", {
    method: "DELETE",
    body: { password, confirmation: "DELETE" },
  });

/**
 * Everything this account holds, as a zip.
 *
 * Not routed through `api()`: the response is a binary archive, and that helper
 * ends in `res.json()`. Refresh-on-401 is not replicated here either — this is
 * a deliberate click, and asking the user to click it again beats a second
 * download path that can silently diverge from the first.
 */
export async function downloadExport() {
  const res = await fetch(`${API_BASE}/api/auth/export/`, {
    credentials: "include",
    headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
  });
  if (!res.ok) throw apiError(`Export failed (${res.status}).`, res.status);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "lattice-export.zip";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
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

async function uploadLedgerFile(accountId, file, action) {
  if (!file) throw new Error("Choose a CSV file first.");
  const form = new FormData();
  form.append("file", file);
  // Through `api()`, not a bare fetch: an access token lives 30 minutes, and a
  // hand-rolled upload surfaced an expired one as an unexplained import error
  // -- no silent refresh, and no session-expired banner either. `downloadExport`
  // opts out because it reads a binary response; this endpoint answers JSON.
  return api(`/api/accounts/${accountId}/imports/${action}/`, {
    method: "POST",
    body: form,
  });
}

export const previewLedgerImport = (accountId, file) =>
  uploadLedgerFile(accountId, file, "preview");
export const commitLedgerImport = (accountId, file) =>
  uploadLedgerFile(accountId, file, "commit");

export const listLiabilities = (accountId) =>
  api(`/api/accounts/${accountId}/liabilities/`);
export const createLiability = (accountId, { label, amountTomans, assetKey = null }) =>
  api(`/api/accounts/${accountId}/liabilities/`, {
    method: "POST",
    body: {
      label,
      amount_tomans: Number(amountTomans),
      ...(assetKey ? { asset_key: assetKey } : {}),
    },
  });
export const updateLiability = (accountId, id, { label, amountTomans, assetKey = null }) =>
  api(`/api/accounts/${accountId}/liabilities/${id}/`, {
    method: "PATCH",
    body: {
      ...(label != null ? { label } : {}),
      ...(amountTomans != null ? { amount_tomans: Number(amountTomans) } : {}),
      ...(assetKey !== undefined ? { asset_key: assetKey || null } : {}),
    },
  });
export const deleteLiability = (accountId, id) =>
  api(`/api/accounts/${accountId}/liabilities/${id}/`, { method: "DELETE" });

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

// `days` is a calendar window, not a row count: the warehouse prints about one
// row a day but the live fallback ticks every two minutes, so a row cap meant
// the same number bought a year of one asset and half a day of another.
export const priceHistory = (assetKey, days = 365) =>
  api(`/api/prices/history/?asset=${encodeURIComponent(assetKey)}&days=${days}`);

// Portfolio against what you could have held instead, indexed to 100.
/**
 * With no `mode`, answers what the pickers may offer; with one, runs that
 * comparison. `days` omitted means "as far back as my own history goes".
 */
export const comparison = (account = null, { mode, subject, target, days } = {}) => {
  const params = new URLSearchParams();
  if (account) params.set("account", account);
  if (mode) params.set("mode", mode);
  if (subject) params.set("subject", subject);
  if (target) params.set("target", target);
  if (days != null) params.set("days", String(days));
  const qs = params.toString();
  return api(`/api/comparison/${qs ? `?${qs}` : ""}`);
};

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
// `maxAssets` caps how many positions the answer may hold; `targetVolatility`
// is risk tolerance as an annualized number (0.25 = 25%). Omit either for none.
export const myOptimal = (account = null, { maxAssets, targetVolatility } = {}) => {
  const params = new URLSearchParams(accountParam(account) || "");
  if (maxAssets != null) params.set("max_assets", String(maxAssets));
  if (targetVolatility != null) params.set("target_volatility", String(targetVolatility));
  const s = params.toString();
  return api(`/api/optimization/my-optimal/${s ? `?${s}` : ""}`);
};
// ~200 bootstrap re-solves. Never fetched with the page — the caller gates it
// behind an explicit request and gives it a longer timeout than the default.
export const robustness = (account = null, { scenario, window, targetVolatility } = {}) => {
  const params = new URLSearchParams(accountParam(account) || "");
  if (scenario) params.set("scenario", scenario);
  if (window != null) params.set("window", String(window));
  if (targetVolatility != null) params.set("target_volatility", String(targetVolatility));
  const s = params.toString();
  return api(`/api/optimization/robustness/${s ? `?${s}` : ""}`);
};
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

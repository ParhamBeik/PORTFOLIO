// Browser-side latency telemetry, rolled up server-side by the `perf` app.
//
// Three measurements, all batched and sent with sendBeacon (never blocking a
// request the user is waiting on):
//
//   api  — every `api()` call: wall time as the user experienced it, which
//          includes queueing at nginx and the network, unlike the server's own
//          timing of the same route.
//   page — navigation to "the page has its data": from a route change until
//          every initial `useApi` load started on it has settled, and nothing
//          new started for SETTLE_MS. The settle window is what makes a
//          waterfall (a card that only fetches once its parent resolved) count
//          in full instead of reporting the first wave.
//   boot — first page ready after a hard load, measured from navigation start,
//          so it includes the bundle download and the refresh -> /me chain.
//
// The server whitelists page labels and resolves API paths to their route
// pattern, so ids in URLs never become rows.

const ENDPOINT = "/api/perf/client/";
const FLUSH_MS = 15000;
const SETTLE_MS = 300;
const MAX_QUEUE = 200;

let apiBase = "";
let queue = [];
let timer = null;
let booted = false;

const nav = { label: null, start: 0, pending: 0, tracked: 0, lastDone: 0, settle: null };

function now() {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function push(event) {
  if (queue.length >= MAX_QUEUE) return;
  queue.push(event);
  if (!timer && typeof window !== "undefined") {
    timer = window.setTimeout(flush, FLUSH_MS);
  }
}

export function flush() {
  if (timer) {
    window.clearTimeout(timer);
    timer = null;
  }
  if (!queue.length) return;
  const payload = JSON.stringify({ events: queue });
  queue = [];
  const url = `${apiBase}${ENDPOINT}`;
  try {
    const blob = new Blob([payload], { type: "application/json" });
    if (navigator.sendBeacon && !apiBase && navigator.sendBeacon(url, blob)) return;
  } catch {
    // fall through to fetch
  }
  fetch(url, {
    method: "POST",
    body: payload,
    headers: { "Content-Type": "application/json" },
    keepalive: true,
    credentials: "omit",
  }).catch(() => {});
}

export function initPerf(base = "") {
  apiBase = base;
  if (typeof window === "undefined") return;
  const onHide = () => {
    if (document.visibilityState === "hidden") flush();
  };
  document.addEventListener("visibilitychange", onHide);
  window.addEventListener("pagehide", flush);
}

export function recordApi(path, method, status, ms) {
  if (path.startsWith(ENDPOINT)) return;
  push({ kind: "api", route: path, method, status, ms: Math.round(ms) });
}

// Called on every route change with "<pathname>" or "<pathname>:<view>".
export function beginPage(label) {
  if (nav.settle) clearTimeout(nav.settle);
  Object.assign(nav, { label, start: now(), pending: 0, tracked: 0, lastDone: 0, settle: null });
}

function maybeSettle() {
  if (nav.settle) clearTimeout(nav.settle);
  if (nav.pending > 0 || !nav.label || !nav.tracked) return;
  const label = nav.label;
  const start = nav.start;
  nav.settle = setTimeout(() => {
    if (nav.label !== label || nav.pending > 0) return;
    const doneAt = nav.lastDone;
    push({ kind: "page", route: label, ms: Math.round(doneAt - start) });
    if (!booted) {
      booted = true;
      push({ kind: "boot", ms: Math.round(doneAt) });
    }
    nav.label = null; // one measurement per navigation
  }, SETTLE_MS);
}

// Wraps one initial page load request; returns the function to call when it settles.
export function trackLoad() {
  if (!nav.label) return () => {};
  const label = nav.label;
  nav.pending += 1;
  nav.tracked += 1;
  if (nav.settle) clearTimeout(nav.settle);
  let done = false;
  return () => {
    if (done || nav.label !== label) return;
    done = true;
    nav.pending -= 1;
    nav.lastDone = now();
    maybeSettle();
  };
}

export { now as perfNow };

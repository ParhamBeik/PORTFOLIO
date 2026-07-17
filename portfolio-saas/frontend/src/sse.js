// Server-Sent Events client for the live price map.
// EventSource cannot set headers, so the access JWT rides in ?token=.
// Resilience: exponential backoff on error, fall back to 15s polling of
// /api/prices/latest/ after 3 SSE failures, and reconnect when the tab is
// visible again (browsers close SSE on background/sleep).

import { auth } from "./api.js";

const API_BASE = import.meta.env.VITE_API_URL || "";
const POLL_MS = 15000;
const MAX_BACKOFF_MS = 30000;

export function subscribePrices({ onPrices, onOpen, onError } = {}) {
  let es = null;
  let pollTimer = null;
  let backoff = 1000;
  let failures = 0;
  let closed = false;

  const pollOnce = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/prices/latest/`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      });
      if (res.ok) onPrices?.(await res.json());
    } catch {
      /* keep polling */
    }
  };

  const startPolling = () => {
    if (pollTimer) return;
    pollOnce();
    pollTimer = setInterval(pollOnce, POLL_MS);
  };
  const stopPolling = () => {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  };

  const connect = () => {
    if (closed) return;
    const token = auth.token;
    if (!token) {
      startPolling();
      return;
    }
    es = new EventSource(`${API_BASE}/api/prices/stream/?token=${encodeURIComponent(token)}`);

    es.addEventListener("open", () => {
      backoff = 1000;
      failures = 0;
      stopPolling();
      onOpen?.();
    });
    es.addEventListener("hello", (e) => onPrices?.(JSON.parse(e.data)));
    es.addEventListener("price", (e) => onPrices?.(JSON.parse(e.data)));
    es.addEventListener("error", () => {
      es?.close();
      failures += 1;
      if (failures >= 3) {
        startPolling(); // SSE unreliable here; keep data flowing via polling
        onError?.(new Error("SSE unavailable; polling"));
      }
      backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      setTimeout(connect, backoff);
    });
  };

  const onVisible = () => {
    if (document.visibilityState === "visible") {
      es?.close();
      stopPolling();
      failures = 0;
      backoff = 1000;
      connect();
    }
  };
  document.addEventListener("visibilitychange", onVisible);

  connect();

  // Teardown: close the stream, stop polling, drop the listener.
  return () => {
    closed = true;
    es?.close();
    stopPolling();
    document.removeEventListener("visibilitychange", onVisible);
  };
}

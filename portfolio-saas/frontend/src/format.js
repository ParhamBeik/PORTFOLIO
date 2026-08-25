// Formatters. One convention, enforced by signature:
//
//   PERCENTAGES TRAVEL AS FRACTIONS. `pct(0.12)` → "12.0%".
//
// The old `fmtPct` took 0–100, so every call site wrote `fmtPct(v * 100)` and a
// payload that was already percent-scale looked identical to one that wasn't.
// The API is fractions almost everywhere (weights, sharpe, twr, xirr, metrics);
// the lone exception is `rebalance_trades[].delta_weight_pct`, converted at its
// single point of use.
//
// Money is Toman. The backend serializes Decimals as strings — Number() them.

/** User-facing performance metric names (API fields remain twr / xirr). */
export const perfLabel = {
  twr: "Portfolio return",
  xirr: "Personal return",
};

export const PERF_UNLOCK_HINT =
  "Record opening balances on the Ledger page to unlock return metrics.";

const nf = (max) => ({ maximumFractionDigits: max });
const bad = (n) => n === null || n === undefined || n === "" || isNaN(Number(n));

export const num = (n, d = 2) =>
  bad(n) ? "—" : Number(n).toLocaleString("en-US", nf(d));

export const toman = (n) =>
  bad(n) ? "—" : Number(n).toLocaleString("en-US", nf(0)) + " T";

/** @param {number} f fraction, e.g. 0.12 → "12.0%" */
export const pct = (f, d = 1) =>
  bad(f) ? "—" : (Number(f) * 100).toLocaleString("en-US", nf(d)) + "%";

const signed = (fn) => (v) =>
  bad(v) ? "—" : (Number(v) > 0 ? "+" : "") + fn(v);

export const signedPct = signed(pct);
export const signedToman = signed(toman);

/** Compact Toman for chart axes so ticks stay readable. */
export function tomanCompact(n) {
  if (bad(n)) return "—";
  const v = Number(n);
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(0) + "K";
  return String(Math.round(v));
}

// Gregorian, Tehran wall clock — the backend stores UTC, the reader is in Iran.
const dtf = (opts) => new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Tehran", ...opts });

export function date(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "—";
  return dtf({ year: "numeric", month: "short", day: "2-digit" }).format(d);
}

export function dateTime(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "—";
  return dtf({
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

/** Short axis tick: "04 Mar" for near ranges, "2026-03" for long ones. */
export const dateTick = (iso, long = false) => {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "";
  return long
    ? dtf({ year: "numeric", month: "2-digit" }).format(d).replace("/", "-")
    : dtf({ month: "short", day: "2-digit" }).format(d);
};

/** Reason codes and enum values arrive snake_case; render them readably. */
/** Time-series charts: show time when the span is short, month when long. */
export function trendAxisTick(iso, spanMs) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "";
  const twoDays = 2 * 86400000;
  const sixtyDays = 60 * 86400000;
  if (spanMs <= twoDays) {
    return dtf({ month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
  }
  if (spanMs <= sixtyDays) {
    return dtf({ month: "short", day: "2-digit" }).format(d);
  }
  return dtf({ year: "numeric", month: "short" }).format(d);
}

export const humanize = (code) =>
  !code ? "" : String(code).replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());

/** Relative-age label for a freshness timestamp measured in seconds, e.g. "1h 44m ago". */
export function ago(seconds) {
  if (bad(seconds)) return "—";
  const s = Math.max(0, Math.round(Number(seconds)));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) {
    const remM = m % 60;
    return remM ? `${h}h ${remM}m ago` : `${h}h ago`;
  }
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

/** Prefer the Persian name — that is how TSE symbols are recognized. */
export const assetLabel = (a) => a?.name_fa || a?.name || a?.key || "—";

/**
 * What to call one row of someone's portfolio.
 *
 * `label` is the owner's own name for their copy of the asset ("Home", "Dad's
 * gold bar"), resolved server-side against the holding. Everything else falls
 * back to the shared catalog, so a row without a nickname reads exactly as it
 * always did. Rows arrive from three shapes — valuation items, ledger entries
 * and the asset catalog — hence the spread of key names.
 */
export const holdingLabel = (row) =>
  row?.label || row?.name_fa || row?.asset_name_fa || row?.asset ||
  row?.name || row?.asset_name || row?.key || row?.asset_key || "—";

/** Floor area, e.g. "91 m²". */
export const area = (sqm) => (bad(sqm) ? "—" : num(sqm, 2) + " m²");

/** Real-estate unit price: what one square meter costs, e.g. "100,000,000 T / m²". */
export const perSqm = (tomans) => (bad(tomans) ? "—" : toman(tomans) + " / m²");

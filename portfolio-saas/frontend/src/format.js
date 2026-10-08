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

import { translate } from "./i18n.js";
import { jalaliLabel, toJalali } from "./jalali.js";

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

export const rial = (n) =>
  bad(n) ? "—" : Number(n).toLocaleString("en-US", nf(0)) + " ﷼";

/**
 * Money in the basis the user picked, which is NOT always Toman.
 *
 * The API converts the figures; only the label was left behind, so a portfolio
 * switched to USD read "167,579 T" and a $1 note priced at "1 T". Toman rounds to
 * whole units because a single Toman is noise; the foreign bases keep cents,
 * where rounding to the unit is a visible 0.5% error on a small holding.
 */
const BASIS_MONEY = {
  nominal_toman: (v) => v.toLocaleString("en-US", nf(0)) + " T",
  real_toman: (v) => v.toLocaleString("en-US", nf(0)) + " T",
  usd_denominated: (v) => "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  usdt_denominated: (v) => v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " USDT",
};

export const money = (n, basis) =>
  bad(n) ? "—" : (BASIS_MONEY[basis] || BASIS_MONEY.nominal_toman)(Number(n));

/**
 * A unit price in whatever currency the server says it is quoted in.
 *
 * A TSE quote is Rial and is shown that way on purpose (`holding_value_to_toman`
 * divides the product, never the price). Everything else is Toman. Suffixing
 * every price " T" made `quantity x price` come out ten times the Toman `value`
 * printed beside it, so the currency has to come from the payload's
 * `unit_price_currency` rather than from the asset class or the magnitude.
 */
export const unitPrice = (n, currency, basis = "nominal_toman") => {
  // Under a foreign basis the server has already converted the quote, so the
  // Rial/Toman distinction no longer applies -- it is dollars either way.
  if (basis === "usd_denominated" || basis === "usdt_denominated") {
    const v = Number(n);
    // Two decimals is right for a $154 coin and wrong for a 4.6-cent share: a
    // TSE stock converted to dollars rounds $0.046348 to "$0.05", and the 100,000
    // shares beside it turn that 8% into $5,000 against a value column reading
    // $4,634.78. Columns that do not multiply out is the very complaint the
    // Rial/Toman labelling was added to answer, so a sub-unit price keeps enough
    // digits to survive the quantity it is multiplied by.
    if (!bad(v) && v !== 0 && Math.abs(v) < 1) {
      const digits = v.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 6,
      });
      return basis === "usd_denominated" ? "$" + digits : digits + " USDT";
    }
    return money(n, basis);
  }
  return currency === "rial" ? rial(n) : toman(n);
};

/** @param {number} f fraction, e.g. 0.12 → "12.0%" */
export const pct = (f, d = 1) =>
  bad(f) ? "—" : (Number(f) * 100).toLocaleString("en-US", nf(d)) + "%";

const signed = (fn) => (v) =>
  bad(v) ? "—" : (Number(v) > 0 ? "+" : "") + fn(v);

export const signedPct = signed(pct);
export const signedToman = signed(toman);

/**
 * A rebased index point: unitless by construction, so it must not carry the
 * Toman suffix. 100 is the window's start; 130 means up 30%.
 */
export const indexPoint = (n) => (bad(n) ? "—" : num(n, 1));

/** Compact Toman for chart axes so ticks stay readable. */
export function tomanCompact(n, digits = 1) {
  if (bad(n)) return "—";
  const v = Number(n);
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(digits) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(digits) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(0) + "K";
  return String(Math.round(v));
}

/**
 * Compact money for chart axes, in the basis the points are actually in.
 *
 * `tomanCompact` is unconditional, so a chart of a portfolio switched to USD
 * drew converted dollars against a Toman axis and a "T" tooltip — the same
 * relabelling gap that was closed on the total and the holdings rows. Foreign
 * bases keep two decimals below a thousand, where a $154.54 coin rounding to
 * "155" is a visible error rather than noise.
 */
export function moneyCompact(n, basis = "nominal_toman", digits = 1) {
  if (bad(n)) return "—";
  const foreign = basis === "usd_denominated" || basis === "usdt_denominated";
  if (!foreign) return tomanCompact(n, digits);
  const v = Number(n);
  const a = Math.abs(v);
  const mark = basis === "usd_denominated" ? "$" : "";
  const tail = basis === "usdt_denominated" ? " USDT" : "";
  if (a >= 1e9) return mark + (v / 1e9).toFixed(digits) + "B" + tail;
  if (a >= 1e6) return mark + (v / 1e6).toFixed(digits) + "M" + tail;
  if (a >= 1e3) return mark + (v / 1e3).toFixed(digits) + "K" + tail;
  return money(v, basis);
}

/**
 * Decimals a compact axis needs so neighbouring ticks never print the same
 * label. A flat 1.43B portfolio gets ticks 20M apart, and at one decimal every
 * one of them read "1.4B".
 */
export function compactAxisDigits(max, interval) {
  const a = Math.abs(Number(max));
  const scale = a >= 1e9 ? 1e9 : a >= 1e6 ? 1e6 : a >= 1e3 ? 1e3 : 1;
  const step = Number(interval) / scale;
  if (!(step > 0) || !Number.isFinite(step)) return 1;
  return Math.min(3, Math.max(1, Math.ceil(-Math.log10(step) - 1e-9)));
}

// Gregorian, Tehran wall clock — the backend stores UTC, the reader is in Iran.
const dtf = (opts) => new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Tehran", ...opts });

export function date(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "—";
  return dtf({ year: "numeric", month: "short", day: "2-digit" }).format(d);
}

/**
 * The same instant on the calendar the reader keeps: "7 Shahrivar 1405".
 *
 * Entries are dated on a Persian calendar when they are recorded, so they are
 * read back on one. `dateTime` stays for the hover, where the Gregorian date and
 * the time of day still answer "which of these two came first".
 */
export function jalaliDate(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "—";
  return jalaliLabel(toJalali(d));
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

// Status codes and asset classes reach the screen through this, so it is also
// where they are translated: "stale" -> "Stale" -> its Persian entry.
export const humanize = (code) =>
  !code ? "" : translate(String(code).replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()));

/**
 * A sentence the API wrote, in the reader's language when we know it. Fixed
 * sentences are dictionary entries; the few that carry a number are matched
 * here and re-filled, so the number survives the translation.
 */
const SERVER_PATTERNS = [
  [/^Performance available after (\d+) more day\(s\) of tracking\.$/, "Performance available after {n} more day(s) of tracking."],
];
export function serverText(text) {
  if (typeof text !== "string") return text;
  for (const [re, key] of SERVER_PATTERNS) {
    const m = text.match(re);
    if (m) return translate(key, undefined, { n: m[1] });
  }
  return translate(text);
}

/** Relative-age label for a freshness timestamp measured in seconds, e.g. "1h 44m ago". */
export function ago(seconds) {
  if (bad(seconds)) return "—";
  const s = Math.max(0, Math.round(Number(seconds)));
  // Whole phrases with placeholders, so Persian can order them its own way.
  if (s < 60) return translate("just now");
  const m = Math.floor(s / 60);
  if (m < 60) return translate("{m}m ago", undefined, { m });
  const h = Math.floor(m / 60);
  if (h < 24) {
    const remM = m % 60;
    return remM
      ? translate("{h}h {m}m ago", undefined, { h, m: remM })
      : translate("{h}h ago", undefined, { h });
  }
  const d = Math.floor(h / 24);
  return translate("{d}d ago", undefined, { d });
}

/**
 * Prefer the Persian name — that is how TSE symbols are recognized.
 *
 * The ticker comes first for a stock: `name_fa` holds the REGISTERED COMPANY
 * name ("گسترش‌سرمایه‌گذاری‌ایران‌خودرو"), which nobody uses and which is long
 * enough to break a table row, while `tse_symbol` holds what the owner
 * actually calls it ("خگستر"). Mirrors `Holding.label` on the server, so the
 * catalog pickers and the holdings rows print the same string.
 */
export const assetLabel = (a) =>
  a?.tse_symbol || a?.name_fa || a?.name || a?.key || "—";

/**
 * What to call a row in the catalog picker.
 *
 * Crypto reads the other way round from everything else. The provider's coin
 * feed carries no symbol field at all — the ingest keys those rows on `name_en`,
 * so the row's `symbol` IS the English name of the coin and its `name` is the
 * Persian one. A Persian ticker is how a share is recognized; a coin is known as
 * Bitcoin, not as بیت کوین, so the two are not labelled by the same rule.
 */
export const catalogLabel = (a) =>
  a?.asset_class === "Crypto" ? a?.symbol || a?.name || a?.key || "—" : assetLabel(a);

/** The row's other name, for a hover. Empty when it would only repeat the label. */
export const nativeName = (a) => {
  const other = a?.name_fa || a?.name || "";
  return other === catalogLabel(a) ? "" : other;
};

/**
 * What to call one row of someone's portfolio.
 *
 * `label` is the owner's own name for their copy of the asset ("Home", "Dad's
 * gold bar"), resolved server-side against the holding. Everything else falls
 * back to the shared catalog, so a row without a nickname reads exactly as it
 * always did. Rows arrive from three shapes — valuation items, ledger entries
 * and the asset catalog — hence the spread of key names.
 */
/**
 * Wrap a name in Unicode first-strong isolates before it goes into a sentence.
 *
 * A Persian ticker inside an English sentence ("Sell all of خگستر 2?") takes the
 * neighbouring digits and punctuation with it under the bidi algorithm, so the
 * sentence renders scrambled. FSI…PDI makes the name its own direction island.
 * Plain strings only -- confirm(), aria-label, title -- where `<bdi>` cannot go.
 */
export const isolate = (text) => `\u2068${text}\u2069`;

export const holdingLabel = (row) =>
  row?.label || row?.name_fa || row?.asset_name_fa || row?.asset ||
  row?.name || row?.asset_name || row?.key || row?.asset_key || "—";

/**
 * A held quantity, printed to the precision the asset can actually be held in.
 *
 * `quantity_step` is the server's declaration of what one unit is (see
 * `Asset.quantity_step`): "1" for anything counted — a share, a coin, a bar, a
 * banknote — and a fractional step for the three units that genuinely divide.
 * Printing four decimals on all of them turned 1,200 shares into "1,200.0000"
 * and implied a fraction of a share was a thing you could own.
 */
export const isWholeUnit = (step) => String(step ?? "") === "1";

export const quantity = (n, step) => {
  if (isWholeUnit(step)) return num(n, 0);
  const s = String(step ?? "");
  const dot = s.indexOf(".");
  return num(n, dot < 0 ? 4 : Math.min(8, s.length - dot - 1));
};

/** Floor area, e.g. "91 m²". */
export const area = (sqm) => (bad(sqm) ? "—" : num(sqm, 2) + " m²");

/** Real-estate unit price: what one square meter costs, e.g. "100,000,000 T / m²". */
export const perSqm = (tomans) => (bad(tomans) ? "—" : toman(tomans) + " / m²");

/**
 * A colour per asset class, for every allocation chart in the app. Assigned by
 * rank, Gold was blue on one screen and orange on the next the moment cash
 * outgrew it; a class keeps its colour wherever it appears.
 */
export const CLASS_SLOT = { Gold: 0, Cash: 1, Stock: 2, Crypto: 3, "Real Estate": 4, Other: 5 };

/**
 * Holdings grouped by asset class, for the allocation rings.
 *
 * Account cash joins the "Cash" class (USD, EUR and USDT are already there):
 * it is part of the total above the ring, and a second "Cash" slice beside
 * the first read as two different things. Empty classes are dropped -- an
 * unpriced holding is not a 0% slice. Beyond eight, the tail becomes "Other".
 */
export function allocationByClass(items, cash = 0) {
  const totals = new Map();
  for (const it of items || []) {
    const key = it.class || "Other";
    totals.set(key, (totals.get(key) || 0) + Number(it.value || 0));
  }
  if (Number(cash) > 0) totals.set("Cash", (totals.get("Cash") || 0) + Number(cash));
  const groups = [...totals.entries()]
    .filter(([, value]) => value > 0)
    .map(([key, value]) => ({ name: humanize(key), value, slot: CLASS_SLOT[key] ?? CLASS_SLOT.Other }))
    .sort((a, b) => b.value - a.value);
  if (groups.length <= 8) return groups;
  const rest = groups.slice(7).reduce((sum, g) => sum + g.value, 0);
  return [...groups.slice(0, 7), { name: "Other", value: rest, slot: CLASS_SLOT.Other }];
}

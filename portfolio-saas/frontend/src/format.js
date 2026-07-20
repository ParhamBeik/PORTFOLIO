// Shared number/time formatters. Pulled out of Dashboard so every page formats
// Tomans, quantities and Tehran-time stamps identically.

export function fmtNum(n) {
  if (n === null || n === undefined || n === "" || isNaN(n)) return "0";
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function fmtToman(n) {
  if (n === null || n === undefined || n === "" || isNaN(n)) return "0";
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 }) + " T";
}

// Percent for ratios already in 0..1 scale (weights, vol). "—" for missing.
export function fmtPct(frac, digits = 1) {
  if (frac === null || frac === undefined || frac === "" || isNaN(frac)) return "—";
  return Number(frac).toLocaleString("en-US", { maximumFractionDigits: digits }) + "%";
}

// Compact Toman for chart axes (12.3M / 4.5B) so ticks stay readable.
export function fmtTomanCompact(n) {
  if (n === null || n === undefined || n === "" || isNaN(n)) return "0";
  const v = Number(n);
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (v / 1e3).toFixed(0) + "K";
  return String(v);
}

// Tehran wall-clock time for a fetched-at ISO stamp. The backend stores UTC; show
// the Iranian user their local market time.
export function fmtTehranTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("fa-IR", {
    timeZone: "Asia/Tehran",
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
  }).format(d);
}

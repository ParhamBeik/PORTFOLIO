// Shared number/time formatters. Pulled out of Dashboard so every page formats
// Tomans, quantities and Tehran-time stamps identically.
//
// Every "_tomans"-suffixed API field arrives already in Tomans: the backend
// converts raw Rial TSE warehouse closes via `tse_close_to_toman()` at the read
// boundary, and gold/FX/ledger values are Toman at rest. Do NOT divide by 10
// here — that would understate every displayed amount by 10x.

export function fmtNum(n) {
  if (n === null || n === undefined || n === "" || isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function fmtToman(n) {
  if (n === null || n === undefined || n === "" || isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 }) + " T";
}

// Percent formatter. Callers pass an already percentage-scale number (0-100),
// e.g. fmtPct(v * 100) for a 0..1 fraction — not the raw 0..1 fraction itself.
// "—" for missing.
export function fmtPct(pct, digits = 1) {
  if (pct === null || pct === undefined || pct === "" || isNaN(pct)) return "—";
  return Number(pct).toLocaleString("en-US", { maximumFractionDigits: digits }) + "%";
}

// Compact Toman for chart axes (12.3M / 4.5B) so ticks stay readable.
export function fmtTomanCompact(n) {
  if (n === null || n === undefined || n === "" || isNaN(n)) return "—";
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

// Timeframe-aware date formatter for chart X-axis ticks.
// Handles timestamps (ms), ISO strings, and Jalali date strings (YYYY-MM-DD / YYYY/MM/DD).
export function fmtDateTick(val, timeframe = 7) {
  if (val === null || val === undefined || val === "") return "";

  if (typeof val === "number" || (!isNaN(Number(val)) && !String(val).includes("-") && !String(val).includes("/"))) {
    const ts = Number(val);
    if (isNaN(ts)) return "";
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";

    const days = typeof timeframe === "number" ? timeframe : (timeframe === "1Y" ? 365 : timeframe === "3Y" ? 1095 : 365);

    if (days <= 1) {
      return d.toLocaleTimeString("en-US", { timeZone: "Asia/Tehran", hour: "2-digit", minute: "2-digit", hour12: false });
    }
    if (days <= 30) {
      const month = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${month}/${day}`;
    }
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    return `${year}/${month}`;
  }

  const str = String(val).trim();
  if (/^\d{4}[-/]\d{2}[-/]\d{2}/.test(str)) {
    const parts = str.split(/[-T /]/);
    const yr = parts[0];
    const mo = parts[1];
    const dy = parts[2];
    if (timeframe === "All" || timeframe === "3Y" || timeframe === 3650) {
      return `${yr}/${mo}`;
    }
    return `${mo}/${dy}`;
  }

  return str;
}

// Clean date/time string for chart tooltips
export function fmtChartTooltipDate(val) {
  if (val === null || val === undefined || val === "") return "—";
  if (typeof val === "number" || (!isNaN(Number(val)) && !String(val).includes("-") && !String(val).includes("/"))) {
    const d = new Date(Number(val));
    if (isNaN(d.getTime())) return "—";
    return new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Tehran",
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(d);
  }
  return String(val);
}


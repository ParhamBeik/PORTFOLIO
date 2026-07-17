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

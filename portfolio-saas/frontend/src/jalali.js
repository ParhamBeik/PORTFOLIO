// The Persian (Jalali) calendar, borrowed from the platform.
//
// Intl already ships a correct Persian calendar (`-u-ca-persian`, ICU), so there
// is no leap-year table and no astronomical constant here: Gregorian -> Jalali is
// a format call. The one direction Intl does not offer is Jalali -> Gregorian,
// and that is obtained by walking a guess until the platform agrees with it —
// which also makes "does this day exist?" answerable without a leap rule.
//
// Everything is evaluated on Tehran's wall clock, because that is the calendar
// the reader is on. Run `node src/jalali.check.js` after touching this file.

const DAY_MS = 86_400_000;

//: Iran dropped daylight saving in 2022; the offset has been fixed since. Used
//: only to place a chosen day at its own local midnight, so the pre-2022 summer
//: half-hour cannot push an instant onto the neighbouring day.
const TEHRAN_OFFSET_MS = 3.5 * 60 * 60 * 1000;

/** Transliterated, because the rest of this app's chrome is English. */
export const JALALI_MONTHS = [
  "Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar",
  "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand",
];

/** Saturday first — the Iranian week. Index 0 is the first grid column. */
export const JALALI_WEEKDAYS = ["Sa", "Su", "Mo", "Tu", "We", "Th", "Fr"];

const fmt = new Intl.DateTimeFormat("en-u-ca-persian", {
  timeZone: "Asia/Tehran",
  year: "numeric",
  month: "numeric",
  day: "numeric",
});

/** A Date (or ms) -> `{ jy, jm, jd }` on Tehran's clock. */
export function toJalali(when) {
  const parts = Object.fromEntries(
    fmt.formatToParts(new Date(when)).map((p) => [p.type, p.value])
  );
  return {
    // The Persian year formats as "1405 AP"; the era is not a separate part here.
    jy: Number(String(parts.year).replace(/\D/g, "")),
    jm: Number(parts.month),
    jd: Number(parts.day),
  };
}

// Six 31-day months, then five of 30, then Esfand. Only used to size a step.
const dayOfYear = ({ jm, jd }) => (jm <= 7 ? (jm - 1) * 31 : 186 + (jm - 7) * 30) + jd;

const stepDays = (from, to) => (to.jy - from.jy) * 365 + dayOfYear(to) - dayOfYear(from);

/**
 * `{ jy, jm, jd }` -> the ms timestamp of 00:00 UTC on the matching Gregorian
 * day, or null when that day does not exist (Esfand 30 outside a leap year).
 *
 * 00:00 UTC is 03:30 in Tehran, so the UTC calendar day and the Tehran one are
 * the same day — which is what makes the round-trip check below exact.
 */
export function fromJalali(jy, jm, jd) {
  // 1 Farvardin lands on 20/21 March. The guess is only ever off by accumulated
  // leap drift, and each step removes it, so a handful of rounds is plenty.
  let t = Date.UTC(jy + 621, 2, 21);
  for (let i = 0; i < 8; i += 1) {
    const at = toJalali(t);
    if (at.jy === jy && at.jm === jm && at.jd === jd) return t;
    const step = stepDays(at, { jy, jm, jd });
    // Landed elsewhere yet the step says "you are already there": the requested
    // day is not on the calendar.
    if (step === 0) return null;
    t += step * DAY_MS;
  }
  return null;
}

/** The instant a Jalali day begins in Tehran, as an ISO string, or null. */
export function jalaliToIso(jy, jm, jd) {
  const t = fromJalali(jy, jm, jd);
  return t === null ? null : new Date(t - TEHRAN_OFFSET_MS).toISOString();
}

/** 29, 30 or 31 — Esfand is asked, not computed. */
export const monthLength = (jy, jm) =>
  jm <= 6 ? 31 : jm <= 11 ? 30 : fromJalali(jy, 12, 30) === null ? 29 : 30;

/** Which grid column the 1st of the month sits in, Saturday = 0. */
export function firstColumn(jy, jm) {
  const t = fromJalali(jy, jm, 1);
  if (t === null) return 0;
  return (new Date(t).getUTCDay() + 1) % 7; // getUTCDay: 0 = Sunday, 6 = Saturday
}

/** "7 Shahrivar 1405". */
export const jalaliLabel = ({ jy, jm, jd }) => `${jd} ${JALALI_MONTHS[jm - 1]} ${jy}`;

/** Move a `{ jy, jm }` view by whole months, wrapping the year. */
export function shiftMonth({ jy, jm }, by) {
  const n = (jy * 12 + (jm - 1)) + by;
  return { jy: Math.floor(n / 12), jm: (n % 12) + 1 };
}

export const sameDay = (a, b) =>
  !!a && !!b && a.jy === b.jy && a.jm === b.jm && a.jd === b.jd;

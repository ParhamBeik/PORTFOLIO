// Unit test: tests pure calendar conversion and round-trip consistency across date boundaries,
// providing fast deterministic assertions at the base of the testing pyramid.

import test from "node:test";
import assert from "node:assert/strict";
import {
  toJalali,
  fromJalali,
  JALALI_MONTHS,
  JALALI_WEEKDAYS,
} from "./jalali.js";

test("JALALI_WEEKDAYS starts on Saturday per Iranian convention", () => {
  assert.equal(JALALI_WEEKDAYS[0], "Sa");
  assert.equal(JALALI_WEEKDAYS.length, 7);
  assert.equal(JALALI_MONTHS.length, 12);
  assert.equal(JALALI_MONTHS[0], "Farvardin");
  assert.equal(JALALI_MONTHS[11], "Esfand");
});

test("toJalali converts Gregorian date to Jalali Y-M-D on Tehran clock", () => {
  // 2026-03-21 is around Nowruz 1405-01-01
  const nowruz2026 = new Date("2026-03-21T00:00:00Z");
  const j = toJalali(nowruz2026);
  assert.equal(j.jy, 1405);
  assert.equal(j.jm, 1);
  assert.equal(j.jd, 1);
});

test("fromJalali round-trips with toJalali", () => {
  // Pick standard date: 1405-05-15
  const ms = fromJalali(1405, 5, 15);
  assert.ok(ms !== null, "valid date must return ms timestamp");

  const back = toJalali(ms);
  assert.equal(back.jy, 1405);
  assert.equal(back.jm, 5);
  assert.equal(back.jd, 15);
});

test("fromJalali returns null for non-existent dates", () => {
  // Month 13 does not exist
  assert.equal(fromJalali(1405, 13, 1), null);
  // Month 0 does not exist
  assert.equal(fromJalali(1405, 0, 1), null);
  // Day 32 in 31-day month does not exist
  assert.equal(fromJalali(1405, 1, 32), null);
  // Day 31 in 30-day month (Mehr = month 7) does not exist
  assert.equal(fromJalali(1405, 7, 31), null);
});

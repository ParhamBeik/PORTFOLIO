// Unit test: tests pure mathematical and string formatting logic in isolation,
// the fast base of the testing pyramid, ensuring display consistency without browser overhead.

import test from "node:test";
import assert from "node:assert/strict";
import {
  num,
  toman,
  rial,
  money,
  unitPrice,
  pct,
  pctShare,
  signedPct,
  signedToman,
  indexPoint,
  date,
} from "./format.js";
import { setLang, translate } from "./i18n.js";

test("num formatters handle valid and invalid numbers gracefully", () => {
  assert.equal(num(1234.567, 2), "1,234.57");
  assert.equal(num(0), "0");
  assert.equal(num(null), "—");
  assert.equal(num(undefined), "—");
  assert.equal(num(""), "—");
  assert.equal(num("not_a_number"), "—");
});

test("toman and rial format with correct currency suffixes", () => {
  assert.equal(toman(50000), "\u206650,000 T\u2069");
  assert.equal(toman(0), "\u20660 T\u2069");
  assert.equal(toman(null), "—");

  assert.equal(rial(500000), "\u2066500,000 ﷼\u2069");
  assert.equal(rial(null), "—");
});

test("money formats across currency bases", () => {
  assert.equal(money(12500, "nominal_toman"), "\u206612,500 T\u2069");
  assert.equal(money(12500, "real_toman"), "\u206612,500 T\u2069");
  assert.equal(money(1250.5, "usd_denominated"), "\u2066$1,250.50\u2069");
  assert.equal(money(1250.5, "usdt_denominated"), "\u20661,250.50 USDT\u2069");
  assert.equal(money(null, "usd_denominated"), "—");
});

test("unitPrice formats TSE quotes as rial and others as toman, with USD sub-unit precision", () => {
  assert.equal(unitPrice(3000, "rial", "nominal_toman"), "\u20663,000 ﷼\u2069");
  assert.equal(unitPrice(300, "toman", "nominal_toman"), "\u2066300 T\u2069");

  // USD basis: sub-unit share prices (< 1) keep up to 6 decimals
  assert.equal(unitPrice(0.046348, "rial", "usd_denominated"), "\u2066$0.046348\u2069");
  // Greater than $1 keeps 2 decimals
  assert.equal(unitPrice(154.2, "rial", "usd_denominated"), "\u2066$154.20\u2069");
});

test("pct converts fractions to formatted percentage strings", () => {
  assert.equal(pct(0.12), "12%");
  assert.equal(pct(0.1234, 2), "12.34%");
  assert.equal(pct(0), "0%");
  assert.equal(pct(null), "—");
});

test("signed formatters add + prefix to positive numbers only", () => {
  assert.equal(signedPct(0.05), "+5%");
  assert.equal(signedPct(-0.05), "-5%");
  assert.equal(signedPct(0), "0%");

  assert.equal(signedToman(1000), "+\u20661,000 T\u2069");
  assert.equal(signedToman(-1000), "\u2066-1,000 T\u2069");
});

test("indexPoint formats unitless rebased series points", () => {
  assert.equal(indexPoint(100), "100");
  assert.equal(indexPoint(130.45), "130.5");
  assert.equal(indexPoint(null), "—");
});

test("Persian mode: Jalali dates, Persian digits for counts, context keys", () => {
  assert.equal(translate("Amount|quantity", "en"), "Amount");
  assert.equal(translate("Amount|quantity", "fa"), "تعداد");
  assert.equal(translate("Step {n} of {of}", "fa", { n: 1, of: 5 }), "مرحله ۱ از ۵");
  assert.equal(translate("Step {n} of {of}", "en", { n: 1, of: 5 }), "Step 1 of 5");
  setLang("fa");
  try {
    assert.equal(date("2026-07-20T08:00:00Z"), "29 تیر 1405");
  } finally {
    setLang("en");
  }
  assert.equal(date("2026-07-20T08:00:00Z"), "20 Jul 2026");
});

test("a tiny but real share reads <0.1%, never 0%", () => {
  assert.equal(pctShare(0.00015), "<0.1%");
  assert.equal(pctShare(0), "0%");
  assert.equal(pctShare(0.25), "25%");
  assert.equal(pctShare(null), "—");
});

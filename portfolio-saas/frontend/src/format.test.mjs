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
  signedPct,
  signedToman,
  indexPoint,
} from "./format.js";

test("num formatters handle valid and invalid numbers gracefully", () => {
  assert.equal(num(1234.567, 2), "1,234.57");
  assert.equal(num(0), "0");
  assert.equal(num(null), "—");
  assert.equal(num(undefined), "—");
  assert.equal(num(""), "—");
  assert.equal(num("not_a_number"), "—");
});

test("toman and rial format with correct currency suffixes", () => {
  assert.equal(toman(50000), "50,000 T");
  assert.equal(toman(0), "0 T");
  assert.equal(toman(null), "—");

  assert.equal(rial(500000), "500,000 ﷼");
  assert.equal(rial(null), "—");
});

test("money formats across currency bases", () => {
  assert.equal(money(12500, "nominal_toman"), "12,500 T");
  assert.equal(money(12500, "real_toman"), "12,500 T");
  assert.equal(money(1250.5, "usd_denominated"), "$1,250.50");
  assert.equal(money(1250.5, "usdt_denominated"), "1,250.50 USDT");
  assert.equal(money(null, "usd_denominated"), "—");
});

test("unitPrice formats TSE quotes as rial and others as toman, with USD sub-unit precision", () => {
  assert.equal(unitPrice(3000, "rial", "nominal_toman"), "3,000 ﷼");
  assert.equal(unitPrice(300, "toman", "nominal_toman"), "300 T");

  // USD basis: sub-unit share prices (< 1) keep up to 6 decimals
  assert.equal(unitPrice(0.046348, "rial", "usd_denominated"), "$0.046348");
  // Greater than $1 keeps 2 decimals
  assert.equal(unitPrice(154.2, "rial", "usd_denominated"), "$154.20");
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

  assert.equal(signedToman(1000), "+1,000 T");
  assert.equal(signedToman(-1000), "-1,000 T");
});

test("indexPoint formats unitless rebased series points", () => {
  assert.equal(indexPoint(100), "100");
  assert.equal(indexPoint(130.45), "130.5");
  assert.equal(indexPoint(null), "—");
});

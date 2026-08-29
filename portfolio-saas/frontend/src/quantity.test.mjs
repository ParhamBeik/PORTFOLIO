// Run with: npm run test:unit   (node's built-in runner, no new dependency)
//
// Three forms share these rules and each one previously had its own; the point
// of the checks below is that the bounds match what the server actually accepts,
// so a value that passes here does not come back as a failed save.
import assert from "node:assert/strict";
import { test } from "node:test";
import { QUANTITY_MIN, quantityError, validQuantity } from "./quantity.js";

test("an empty box is not yet an error", () => {
  assert.equal(quantityError(""), "");
  assert.equal(validQuantity(""), false); // ...but it cannot be submitted
});

test("zero is refused by default and allowed only where the server allows it", () => {
  assert.match(quantityError("0"), /greater than zero/);
  assert.equal(quantityError("0", { allowZero: true }), "");
  assert.equal(validQuantity("0", { allowZero: true }), true);
  // Negative stays refused on both paths -- `allow_zero` is not `allow_negative`.
  assert.match(quantityError("-2"), /greater than zero/);
  assert.match(quantityError("-2", { allowZero: true }), /zero or more/);
});

test("the floor matches the server's min_value, and zero is not below it", () => {
  assert.equal(quantityError(String(QUANTITY_MIN)), "");
  assert.match(quantityError("0.0000001"), /smallest quantity/);
  // Regression: the floor check must not fire on the zero `allowZero` permits.
  assert.equal(quantityError("0", { allowZero: true }), "");
});

test("decimal places and magnitude match max_digits=20, decimal_places=6", () => {
  assert.match(quantityError("1.1234567"), /6 decimal places/);
  assert.equal(quantityError("1.123456"), "");
  assert.match(quantityError("1e14"), /larger than we can record/);
  assert.equal(quantityError("99999999999999"), "");
});

test("something that is not a number says so", () => {
  assert.match(quantityError("abc"), /Enter a number/);
});

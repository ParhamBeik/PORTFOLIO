// One rule for "is this a quantity the server will take", because there is more
// than one form that asks for one.
//
// What the server will actually take: `DecimalField(max_digits=20,
// decimal_places=6, min_value=0.000001)`. The add-transaction wizard only asked
// for "> 0", so a quantity of 0.0000001 passed every step, reached the review
// screen and was refused at Save. The onboarding form asked for `min="0"` and so
// accepted a first holding of exactly nothing. Two forms, two different bounds,
// one server -- which is the shape of divergence this module exists to prevent.
import { isWholeUnit } from "./format.js";

export const QUANTITY_MIN = 0.000001;
export const QUANTITY_MAX = 1e14; // max_digits 20 - decimal_places 6

export const positive = (v) => v !== "" && Number(v) > 0;

/**
 * `allowZero` mirrors the server's own `_decimal(allow_zero=)` flag, and exists
 * for exactly the case the server allows it: a holding with no ledger history is
 * disposed of by setting it to nothing. Everywhere else zero is meaningless —
 * a buy of no shares, a property worth nothing per square meter — and the server
 * answers "quantity must be positive" after a round trip.
 *
 * `step` is the server's own declaration of whether one of this asset divides
 * (`Asset.quantity_step`): "1" for a share, a coin or a banknote, a fraction for
 * crypto, gold by the gram and tether. Half a coin is not a thing the market can
 * settle, so it is refused here rather than rounded on save — rounding would
 * change a number the user typed without telling them. Omitting `step` keeps the
 * old behaviour, so a caller that has no asset in hand is unaffected.
 */
export const quantityError = (v, { allowZero = false, step } = {}) => {
  if (v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return "Enter a number.";
  if (allowZero ? n < 0 : n <= 0) {
    return allowZero
      ? "Enter a quantity of zero or more."
      : "Enter a quantity greater than zero.";
  }
  if (isWholeUnit(step) && !Number.isInteger(n)) {
    return "This one is counted in whole units — enter a whole number.";
  }
  if (n !== 0 && n < QUANTITY_MIN) return `The smallest quantity we record is ${QUANTITY_MIN}.`;
  if (n >= QUANTITY_MAX) return "That quantity is larger than we can record.";
  if ((String(v).split(".")[1] || "").length > 6) return "At most 6 decimal places.";
  return "";
};

export const validQuantity = (v, opts) =>
  v !== "" && !quantityError(v, opts);

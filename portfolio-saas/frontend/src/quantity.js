// One rule for "is this a quantity the server will take", because there is more
// than one form that asks for one.
//
// What the server will actually take: `DecimalField(max_digits=20,
// decimal_places=6, min_value=0.000001)`. The add-transaction wizard only asked
// for "> 0", so a quantity of 0.0000001 passed every step, reached the review
// screen and was refused at Save. The onboarding form asked for `min="0"` and so
// accepted a first holding of exactly nothing. Two forms, two different bounds,
// one server -- which is the shape of divergence this module exists to prevent.

export const QUANTITY_MIN = 0.000001;
export const QUANTITY_MAX = 1e14; // max_digits 20 - decimal_places 6

export const positive = (v) => v !== "" && Number(v) > 0;

export const quantityError = (v) => {
  if (v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return "Enter a quantity greater than zero.";
  if (n < QUANTITY_MIN) return `The smallest quantity we record is ${QUANTITY_MIN}.`;
  if (n >= QUANTITY_MAX) return "That quantity is larger than we can record.";
  if ((String(v).split(".")[1] || "").length > 6) return "At most 6 decimal places.";
  return "";
};

export const validQuantity = (v) => positive(v) && !quantityError(v);

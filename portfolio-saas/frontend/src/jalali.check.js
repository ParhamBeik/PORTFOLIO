// Self-check for jalali.js: `node src/jalali.check.js`.
//
// Unit-level, because the conversion is pure arithmetic over the platform's own
// calendar — no component, no network, nothing to stand up. The Jalali -> Gregorian
// direction is a search, so it is the round trip and the anchors that prove it,
// not a re-implementation of the same formula in the assertions.
import assert from "node:assert/strict";
import {
  firstColumn,
  fromJalali,
  jalaliToIso,
  monthLength,
  toJalali,
} from "./jalali.js";

// Dates every Iranian reader knows, so a wrong answer is recognisable on sight.
const ANCHORS = [
  ["2021-03-21T00:00:00Z", 1400, 1, 1],  // Nowruz 1400
  ["2024-03-20T00:00:00Z", 1403, 1, 1],  // Nowruz 1403 — one day earlier
  ["1979-02-11T00:00:00Z", 1357, 11, 22], // 22 Bahman
  ["2026-08-29T00:00:00Z", 1405, 6, 7],
];

for (const [iso, jy, jm, jd] of ANCHORS) {
  assert.deepEqual(toJalali(new Date(iso)), { jy, jm, jd }, iso);
  assert.equal(fromJalali(jy, jm, jd), Date.parse(iso), `${jy}-${jm}-${jd}`);
}

// A chosen day is recorded at ITS OWN midnight in Tehran, which is 20:30 UTC the
// day before. Getting this wrong shifts every entry onto the neighbouring day.
assert.equal(jalaliToIso(1405, 6, 7), "2026-08-28T20:30:00.000Z");

// Esfand is 30 days only in a leap year, and the non-existent day is refused
// rather than silently rolled into Farvardin.
assert.equal(monthLength(1403, 12), 30, "1403 is a leap year");
assert.equal(monthLength(1404, 12), 29);
assert.equal(fromJalali(1404, 12, 30), null);

// Every day of a decade round-trips, and consecutive days are consecutive — the
// two ways a stepping search fails (a day it cannot reach, or one it reaches twice).
let previous = null;
let days = 0;
for (let jy = 1398; jy <= 1408; jy += 1) {
  for (let jm = 1; jm <= 12; jm += 1) {
    for (let jd = 1; jd <= monthLength(jy, jm); jd += 1) {
      const t = fromJalali(jy, jm, jd);
      assert.notEqual(t, null, `${jy}-${jm}-${jd} unreachable`);
      assert.deepEqual(toJalali(t), { jy, jm, jd }, `${jy}-${jm}-${jd} round trip`);
      if (previous !== null) {
        assert.equal(t - previous, 86400000, `gap before ${jy}-${jm}-${jd}`);
      }
      previous = t;
      days += 1;
    }
  }
}

// The grid starts on Saturday, not Sunday: 1 Farvardin 1400 (21 March 2021) was
// a Sunday, so it belongs in the second column, and 27 Esfand 1399 in the first.
assert.equal(firstColumn(1400, 1), 1);
const eve = fromJalali(1399, 12, monthLength(1399, 12));
assert.equal(new Date(eve).getUTCDay(), 6, "the eve of Nowruz 1400 was a Saturday");
assert.equal(firstColumn(1399, 12), (new Date(fromJalali(1399, 12, 1)).getUTCDay() + 1) % 7);

console.log(`jalali.js ok — ${ANCHORS.length} anchors, ${days} days round-tripped`);

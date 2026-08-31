// Every colour we print text in must clear WCAG AA, in both modes.
//
// This exists because the design tokens drifted into two jobs. `--c-good` and
// friends were written as FILL colours (the 3:1 bar for a large coloured area)
// and then used as TEXT through `toneClass`, which `Delta` runs every signed
// number in the app through. Measured before the split: light-mode profit
// figures were 3.15:1, dark-mode loss figures 4.02:1, amber-on-white 1.72:1,
// and white-on-accent (the primary button, on the sign-in page) 3.64:1.
//
// Lighthouse would catch this, but only on the pages it happens to load and
// only once a browser is running. This reads the tokens straight out of
// index.css, so it is deterministic, runs in milliseconds inside the existing
// `npm run test:unit`, and fails on the commit that introduces the regression
// rather than on the deploy after it.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const css = readFileSync(fileURLToPath(new URL("./index.css", import.meta.url)), "utf8");

/** Token values from `:root`, then the same block re-read under the light media query. */
function tokens(mode) {
  // The light overrides live in the only `@media (prefers-color-scheme: light)`
  // block; everything before it is the dark baseline.
  const split = css.indexOf("@media (prefers-color-scheme: light)");
  const source = mode === "dark" ? css.slice(0, split) : css.slice(split);
  const found = {};
  for (const [, name, value] of source.matchAll(/(--c-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})/g)) {
    found[name] = value;
  }
  // Light only overrides what changes, so anything it does not restate is
  // inherited from the dark baseline — mirroring how the cascade resolves it.
  if (mode === "light") {
    for (const [, name, value] of css.slice(0, split).matchAll(/(--c-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})/g)) {
      if (!(name in found)) found[name] = value;
    }
  }
  return found;
}

const channel = (c) => {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
};

function luminance(hex) {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// AA for text under 18.66px. Everything audited here is body copy, table cells
// and badge labels, so the large-text 3:1 allowance never applies.
const AA = 4.5;

// The three surfaces any of this text can land on.
const SURFACES = ["--c-bg", "--c-panel", "--c-panel-2"];

// Foregrounds that carry glyphs. The display twins (`--c-good` and friends) are
// deliberately absent: those are fills, tints and chart series, and holding them
// to a text bar is what would push the palette flat.
const TEXT_TOKENS = [
  "--c-text",
  "--c-muted",
  "--c-accent-text",
  "--c-good-text",
  "--c-warn-text",
  "--c-serious-text",
  "--c-critical-text",
];

// Solid fills that carry white text: the primary button, the success button and
// the active tab.
const WHITE_ON_FILL = ["--c-accent-fill", "--c-good-fill"];

for (const mode of ["dark", "light"]) {
  const t = tokens(mode);

  test(`${mode}: text tokens clear AA on every surface`, () => {
    for (const fg of TEXT_TOKENS) {
      assert.ok(t[fg], `${fg} is not defined in ${mode} mode`);
      for (const bg of SURFACES) {
        const ratio = contrast(t[fg], t[bg]);
        assert.ok(
          ratio >= AA,
          `${fg} (${t[fg]}) on ${bg} (${t[bg]}) is ${ratio.toFixed(2)}:1 in ${mode} mode, needs ${AA}:1`
        );
      }
    }
  });

  test(`${mode}: white text clears AA on solid fills`, () => {
    for (const fill of WHITE_ON_FILL) {
      assert.ok(t[fill], `${fill} is not defined in ${mode} mode`);
      const ratio = contrast("#ffffff", t[fill]);
      assert.ok(
        ratio >= AA,
        `white on ${fill} (${t[fill]}) is ${ratio.toFixed(2)}:1 in ${mode} mode, needs ${AA}:1`
      );
    }
  });

  // Non-text contrast: a chart series only has to be distinguishable from what
  // it sits on, which is the 3:1 bar in WCAG 1.4.11.
  //
  // Light mode does not hold that bar for all eight, and that is deliberate and
  // written down — index.css states three slots sit under 3:1 against white
  // because the palette was solved for CVD separation first, and the charts
  // using them ship direct labels or a table view alongside. So the invariant
  // worth guarding is not "all eight pass", which would force a repalette; it is
  // "no MORE than the documented three fall below", which catches the next
  // colour edit that quietly makes it four.
  const LIGHT_SUB_3_ALLOWANCE = 3;

  test(`${mode}: chart series separate from the panel they draw on`, () => {
    const weak = [];
    for (let i = 1; i <= 8; i += 1) {
      const key = `--c-s${i}`;
      assert.ok(t[key], `${key} is not defined in ${mode} mode`);
      if (contrast(t[key], t["--c-panel"]) < 3) weak.push(key);
    }
    const allowed = mode === "light" ? LIGHT_SUB_3_ALLOWANCE : 0;
    assert.ok(
      weak.length <= allowed,
      `${mode} mode has ${weak.length} series colours under 3:1 against the panel ` +
        `(${weak.join(", ")}); the documented allowance is ${allowed}. Either restore the ` +
        `contrast or update index.css and this allowance together, deliberately.`
    );
  });
}

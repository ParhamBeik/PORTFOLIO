// Fails when a label added in English has no Persian entry. The states these
// strings appear in (a filing, a stock split, a server refusal) rarely occur on
// demo data, so walking the pages in Persian kept reporting them as clean.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(fileURLToPath(import.meta.url));
const dictionary = readFileSync(join(root, "i18n.fa.js"), "utf8");
// Staff-only and unrouted pages stay English.
const SKIP = /spec|Ops\.jsx|BestOverall|MyOptimal/;
const LABEL =
  /(?:\b(?:title|subtitle|hint|empty|summary|caption|header|blurb|label)\s*[=:]\s*\{?|\b(?:translate|tr|t)\(\s*)"([^"\n]*[A-Za-z]{2}[^"\n]*)"/g;
const EMPTY = /<Empty[^>]*>\s*([A-Z][^<>{}]+?)\s*<\/Empty>/g;

const known = (key) =>
  dictionary.includes(`"${key}":`) || new RegExp(`^\\s*${key.replace(/[^\w]/g, "\\$&")}:`, "m").test(dictionary);

test("every interface label has a Persian entry", () => {
  const missing = new Set();
  for (const dir of ["", "pages", "components"]) {
    for (const name of readdirSync(join(root, dir))) {
      if (!name.endsWith(".jsx") || SKIP.test(name)) continue;
      const source = readFileSync(join(root, dir, name), "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/^\s*\/\/.*$/gm, "");
      for (const re of [LABEL, EMPTY]) {
        for (const match of source.matchAll(re)) {
          if (!known(match[1])) missing.add(`${name}: ${match[1]}`);
        }
      }
    }
  }
  assert.deepEqual([...missing], []);
});

// The check Vite does not do.
//
// Vite/Rollup resolve *module* specifiers, not identifiers: a component that
// references a name nothing imported builds clean, ships, and throws
// ReferenceError in the browser the moment that branch renders. Nothing in this
// repo caught that class of bug, and it has shipped before. `no-undef` is the
// whole reason this file exists; everything else here is the small set of rules
// that pay for themselves on a React codebase.
//
// Deliberately narrow. No stylistic rules — formatting is not litigated here,
// and a lint run that mostly prints opinions gets ignored, which costs us the
// one rule that matters.
import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  { ignores: ["dist/**", "node_modules/**", "ios/**", "android/**", ".lighthouseci/**", "test-results/**", "graphify-out/**"] },
  js.configs.recommended,
  {
    files: ["**/*.{js,jsx,mjs}"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.es2024 },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { react, "react-hooks": reactHooks },
    rules: {
      // Without these two, every imported component reads as an unused
      // variable: core ESLint does not know that `<Card/>` is a reference to
      // `Card`. They are the only rules taken from eslint-plugin-react.
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "error",
      // Calling a hook conditionally or inside a loop breaks React's hook
      // ordering — a real crash, not a style question.
      "react-hooks/rules-of-hooks": "error",
      // A stale closure over props/state is the other silent React bug: the
      // effect keeps using the value it captured. Warn rather than error —
      // some deps are omitted deliberately — but make it visible.
      "react-hooks/exhaustive-deps": "warn",
      // Here so an unused import cannot quietly become an unused *variable*
      // left behind by a half-finished edit.
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  {
    // Vitest injects describe/it/expect as globals (vitest.config.js sets
    // `globals: true`), and the node:test files run under Node, not a browser.
    files: ["**/*.spec.{js,jsx}", "**/*.test.mjs", "src/test-setup.js"],
    languageOptions: { globals: { ...globals.node, ...globals.vitest } },
  },
  {
    // Playwright specs and the build/audit scripts are Node programs.
    files: ["e2e/**/*.js", "scripts/**/*.mjs", "probe.mjs", "*.config.js"],
    languageOptions: { globals: { ...globals.node } },
  },
];

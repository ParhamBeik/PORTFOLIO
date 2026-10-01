import { useSyncExternalStore } from "react";
import FA from "./i18n.fa.js";

/**
 * Persian (RTL) and English. English text is the key: a string with no
 * Persian entry renders in English rather than as a blank or a key name, so a
 * new screen is never broken by a missing translation, only untranslated.
 *
 * A tiny external store rather than a context provider: the language changes
 * rarely, every primitive reads it, and the choice must also set `dir` on the
 * document, which no React tree owns.
 */
const KEY = "lang";
const listeners = new Set();

function initial() {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "fa" || saved === "en") return saved;
  } catch {
    /* storage blocked: fall through */
  }
  // Follow the browser until the user picks: a Persian-language browser gets
  // Persian, everything else (including test runners) English.
  const nav = typeof navigator !== "undefined" ? navigator.language || "" : "";
  return nav.toLowerCase().startsWith("fa") ? "fa" : "en";
}

let lang = initial();

function apply() {
  if (typeof document === "undefined") return;
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "fa" ? "rtl" : "ltr";
}
apply();

export function setLang(next) {
  if (next === lang) return;
  lang = next;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* not persisted, still applied */
  }
  apply();
  listeners.forEach((fn) => fn());
}

const subscribe = (fn) => {
  listeners.add(fn);
  return () => listeners.delete(fn);
};

export const useLang = () => useSyncExternalStore(subscribe, () => lang, () => lang);

/** Translate an English UI string; anything that is not a string passes through. */
export function translate(text, to = lang) {
  if (to !== "fa" || typeof text !== "string") return text;
  return FA[text] ?? text;
}

/** Hook form: re-renders the caller when the language changes. */
export function useT() {
  const current = useLang();
  return (text) => translate(text, current);
}

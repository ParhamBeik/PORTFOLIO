import { setLang, useLang } from "../i18n.js";

/** فا / EN. Persian flips the whole document to right-to-left. */
export default function LanguageToggle() {
  const lang = useLang();
  const next = lang === "fa" ? "en" : "fa";
  return (
    <button
      type="button"
      onClick={() => setLang(next)}
      data-testid="lang-toggle"
      aria-label={next === "fa" ? "نمایش به فارسی" : "Show in English"}
      className="app-header-btn inline-flex items-center justify-center rounded-md border border-border bg-panel-2 px-2.5 py-1.5 text-sm font-medium text-text"
    >
      {next === "fa" ? "فا" : "EN"}
    </button>
  );
}


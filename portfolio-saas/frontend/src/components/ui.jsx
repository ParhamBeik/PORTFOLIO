// Shared primitives. Pages compose these and write no bespoke panel, loading or
// error markup — that duplication is what made the old frontend inconsistent.
//
// Every element that a test or a screen reader needs to find takes a `testId`,
// rendered as `data-testid`. Naming convention: "<page>-<thing>", lowercase and
// hyphenated, e.g. "dashboard-total", "optimal-window-tabs".

import { useEffect, useRef, useState } from "react";

import {
  JALALI_MONTHS,
  JALALI_WEEKDAYS,
  firstColumn,
  jalaliLabel,
  jalaliToIso,
  monthLength,
  sameDay,
  shiftMonth,
  toJalali,
} from "../jalali.js";

// Text-safe twins, not the display colors. `Delta` runs every signed number in
// the app through this map, so a 3.15:1 token here is a contrast failure on
// every P&L cell at once. See index.css for how the twins are derived.
const tone = {
  neutral: "text-text",
  muted: "text-muted",
  good: "text-[var(--c-good-text)]",
  warn: "text-[var(--c-warn-text)]",
  serious: "text-[var(--c-serious-text)]",
  critical: "text-[var(--c-critical-text)]",
};

export const toneFor = (n) =>
  n == null || isNaN(n) ? "muted" : Number(n) > 0 ? "good" : Number(n) < 0 ? "critical" : "neutral";

/** Text class for a tone key — for colouring a value inside a table cell. */
export const toneClass = (key) => tone[key] || tone.neutral;

/** Signed value in a table cell, coloured by sign. */
export const Delta = ({ value, format }) => (
  <span className={toneClass(toneFor(value))}>{format(value)}</span>
);

/* ---------------------------------------------------------------- surfaces */

export function Card({ title, subtitle, actions, children, testId, className = "" }) {
  return (
    <section
      data-testid={testId}
      className={`rounded-xl border border-border bg-panel p-5 ${className}`}
    >
      {(title || actions) && (
        <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            {title && <h2 className="text-base font-semibold">{title}</h2>}
            {subtitle && <p className="mt-1 max-w-prose text-sm text-muted">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

/** `size="lg"` gives one hero number more visual weight — same tokens, bigger type. */
export function StatTile({ label, value, sub, valueTone = "neutral", size = "md", testId }) {
  return (
    <div
      data-testid={testId}
      className={`rounded-lg border border-border bg-panel-2 px-4 ${size === "lg" ? "py-4" : "py-3"}`}
    >
      <div className="text-xs font-medium tracking-wide text-muted uppercase">{label}</div>
      <div className={`mt-1 font-semibold ${size === "lg" ? "text-4xl" : "text-2xl"} ${tone[valueTone]}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
  );
}

/** Status wears an icon-free but always-labelled chip — never hue alone. */
export function Badge({ children, variant = "neutral", title, testId }) {
  // Border and tint keep the display color; only the label takes the text twin.
  const styles = {
    neutral: "border-border bg-panel-2 text-muted",
    good: "border-[var(--c-good)]/40 bg-[var(--c-good)]/10 text-[var(--c-good-text)]",
    warn: "border-[var(--c-warn)]/40 bg-[var(--c-warn)]/10 text-[var(--c-warn-text)]",
    serious: "border-[var(--c-serious)]/40 bg-[var(--c-serious)]/10 text-[var(--c-serious-text)]",
    critical: "border-[var(--c-critical)]/40 bg-[var(--c-critical)]/10 text-[var(--c-critical-text)]",
  };
  return (
    <span
      data-testid={testId}
      title={title}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${styles[variant]}`}
    >
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ inputs */

export function Button({ variant = "ghost", className = "", ...props }) {
  // `-fill` on the two solid variants: white on the display accent is 3.64:1 and
  // white on the display green is 3.35:1, and this is the primary call to action
  // on every screen including the sign-in page.
  const styles = {
    primary: "bg-[var(--c-accent-fill)] text-white hover:opacity-90 border-transparent",
    success: "bg-[var(--c-good-fill)] text-white hover:opacity-90 border-transparent",
    ghost: "bg-panel-2 text-text hover:bg-border border-border",
    danger: "bg-transparent text-[var(--c-critical-text)] hover:bg-[var(--c-critical)]/10 border-transparent",
    link: "bg-transparent text-[var(--c-accent-text)] underline underline-offset-2 border-transparent px-1 py-0",
  };
  return (
    <button
      type="button"
      className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${styles[variant]} ${className}`}
      {...props}
    />
  );
}

export function Select({ label, className = "", ...props }) {
  return (
    <select
      aria-label={label}
      className={`rounded-md border border-border bg-panel-2 px-2.5 py-1.5 text-sm text-text ${className}`}
      {...props}
    />
  );
}

export function Input({ label, className = "", ...props }) {
  return (
    <input
      aria-label={label}
      className={`rounded-md border border-border bg-panel-2 px-2.5 py-1.5 text-sm text-text placeholder:text-muted ${className}`}
      {...props}
    />
  );
}

/**
 * A day, chosen on the calendar the reader actually uses.
 *
 * `<input type="datetime-local">` renders whatever calendar the browser's locale
 * says — Gregorian, in an app whose users date everything in Farvardin and Esfand
 * — and it renders it in a chrome we cannot theme. This is a plain grid instead:
 * one month at a time, Saturday first, no popover to clip against the dialog's
 * own scroller.
 *
 * `value` is an ISO instant or "" (meaning "when it is saved"); `onChange` gets
 * the same. A day is recorded at its own midnight in Tehran. Days after today are
 * dead: the server refuses a future entry, so offering one is offering a 400.
 */
export function JalaliDateField({ value, onChange, testId, todayLabel = "Today" }) {
  const today = toJalali(Date.now());
  const selected = value ? toJalali(new Date(value)) : null;
  const [view, setView] = useState(() => selected || today);

  const days = monthLength(view.jy, view.jm);
  const blanks = firstColumn(view.jy, view.jm);
  const isFuture = (jd) =>
    view.jy > today.jy ||
    (view.jy === today.jy &&
      (view.jm > today.jm || (view.jm === today.jm && jd > today.jd)));

  const pick = (jd) => onChange(sameDay(selected, { ...view, jd }) ? "" : jalaliToIso(view.jy, view.jm, jd));
  const step = (by) => setView((v) => shiftMonth(v, by));

  return (
    <div
      data-testid={testId}
      className="rounded-lg border border-border bg-panel-2 p-3"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <Button
          onClick={() => step(-1)}
          aria-label="Previous month"
          data-testid={testId ? `${testId}-prev` : undefined}
        >
          ‹
        </Button>
        <span className="text-sm font-medium" data-testid={testId ? `${testId}-month` : undefined}>
          {JALALI_MONTHS[view.jm - 1]} {view.jy}
        </span>
        <Button
          onClick={() => step(1)}
          aria-label="Next month"
          data-testid={testId ? `${testId}-next` : undefined}
        >
          ›
        </Button>
      </div>

      <div className="grid grid-cols-7 gap-1 text-center text-[0.625rem] font-semibold tracking-wide text-muted uppercase">
        {JALALI_WEEKDAYS.map((d) => (
          <span key={d}>{d}</span>
        ))}
      </div>

      <div className="mt-1 grid grid-cols-7 gap-1">
        {Array.from({ length: blanks }, (_, i) => <span key={`b${i}`} />)}
        {Array.from({ length: days }, (_, i) => i + 1).map((jd) => {
          const isSelected = sameDay(selected, { ...view, jd });
          const isToday = sameDay(today, { ...view, jd });
          return (
            <button
              key={jd}
              type="button"
              disabled={isFuture(jd)}
              aria-pressed={isSelected}
              onClick={() => pick(jd)}
              data-testid={testId ? `${testId}-day-${jd}` : undefined}
              className={`rounded-md border py-1.5 text-sm transition-colors disabled:cursor-not-allowed disabled:border-transparent disabled:text-muted/40 ${
                isSelected
                  ? "border-accent bg-[var(--c-accent-fill)] text-white"
                  : isToday
                    ? "border-accent/50 bg-panel text-text"
                    : "border-transparent text-text hover:bg-panel"
              }`}
            >
              {jd}
            </button>
          );
        })}
      </div>

      <div className="mt-3 flex items-center justify-between gap-2 border-t border-border pt-2">
        <span className="text-xs text-muted" data-testid={testId ? `${testId}-summary` : undefined}>
          {selected ? jalaliLabel(selected) : todayLabel}
        </span>
        {selected && (
          <Button
            variant="link"
            onClick={() => onChange("")}
            data-testid={testId ? `${testId}-clear` : undefined}
          >
            Clear
          </Button>
        )}
      </div>
    </div>
  );
}

/** Segmented control. `options` is [{ value, label, disabled }]. */
export function Tabs({ options, value, onChange, label, testId }) {
  return (
    <div
      role="group"
      aria-label={label}
      data-testid={testId}
      className="inline-flex flex-wrap gap-1 rounded-lg border border-border bg-panel-2 p-1"
    >
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          disabled={o.disabled}
          aria-pressed={value === o.value}
          data-testid={testId ? `${testId}-${o.value}` : undefined}
          onClick={() => onChange(o.value)}
          className={`rounded-md px-3 py-1 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-35 ${
            value === o.value ? "bg-[var(--c-accent-fill)] text-white" : "text-muted hover:text-text"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ tables */

/**
 * `columns` is [{ key, header, align?, render?(row), width? }].
 * Numeric columns should pass align:"right" so the tabular figures line up.
 *
 * `rowClass(row)` styles a whole row by its state — dimming one that has been
 * switched off, say. Per row rather than per cell so the treatment cannot drift
 * between columns.
 */
export function Table({ columns, rows, rowKey, empty = "No rows.", testId, caption, rowClass }) {
  if (!rows?.length) return <Empty testId={testId ? `${testId}-empty` : undefined}>{empty}</Empty>;
  return (
    <div className="overflow-x-auto">
      <table data-testid={testId} className="w-full text-sm">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-border text-left">
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                style={c.width ? { width: c.width } : undefined}
                className={`px-3 py-2 text-xs font-medium tracking-wide text-muted uppercase ${
                  c.align === "right" ? "text-right" : ""
                }`}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={rowKey ? rowKey(row) : i}
              data-testid={testId ? `${testId}-row` : undefined}
              className={`border-b border-border/60 last:border-0 hover:bg-panel-2 ${
                rowClass?.(row) || ""
              }`}
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={`px-3 py-2 ${c.align === "right" ? "text-right" : ""}`}
                >
                  {c.render ? c.render(row) : row[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Previous / next over a paged list. `count` is the TOTAL number of rows, not
 * the number on this page. Works for a server-paged table (Ops) and a
 * client-sliced one (Ledger) alike -- the caller owns which rows it shows.
 */
export function Pager({ page, count, pageSize = 25, onPage, testId }) {
  const pages = Math.max(1, Math.ceil((count || 0) / pageSize));
  return (
    <div data-testid={testId} className="mt-3 flex items-center gap-2 text-sm">
      <Button disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</Button>
      <span className="text-muted">Page {page} / {pages}</span>
      <Button disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</Button>
    </div>
  );
}

/* ------------------------------------------------------------------ states */

export const Loading = ({ children = "Loading…", testId }) => (
  <p role="status" data-testid={testId} className="py-8 text-center text-sm text-muted">
    {children}
  </p>
);

export const Empty = ({ children, action, testId }) => (
  <div data-testid={testId} className="py-8 text-center text-sm text-muted">
    <p>{children}</p>
    {action && <div className="mt-3">{action}</div>}
  </div>
);

export const ErrorState = ({ error, onRetry, testId }) => (
  <div
    role="alert"
    data-testid={testId}
    className="rounded-lg border border-[var(--c-critical)]/40 bg-[var(--c-critical)]/10 px-4 py-3 text-sm"
  >
    <span>{error?.message || "Something went wrong."}</span>
    {onRetry && (
      <Button variant="link" onClick={onRetry} className="ml-2">
        Retry
      </Button>
    )}
  </div>
);

/**
 * Renders the right state for a `useApi` result, so no page repeats the
 * loading / error / empty ladder.
 *
 *   <Async {...state}>{(data) => <Table … />}</Async>
 */
export function Async({ data, error, loading, reload, children, empty, testId }) {
  if (error) return <ErrorState error={error} onRetry={reload} testId={testId} />;
  if (loading && data == null) return <Loading testId={testId} />;
  if (data == null) return <Empty testId={testId}>{empty || "No data yet."}</Empty>;
  return children(data);
}

/**
 * Centred dialog over a scrim. Escape and a backdrop click both close it, and
 * focus moves inside on open so a keyboard user is not left behind on the page.
 *
 * `footer` is pinned below the scrolling body: a stepper's Back/Next must stay
 * reachable when the step is taller than the viewport.
 */
/** Panel widths. `wide` is for dialogs that browse a list rather than confirm
 *  one thing -- the asset picker scrolls a market catalog and 32rem left it
 *  showing roughly three rows. */
const MODAL_WIDTHS = { default: "max-w-lg", wide: "max-w-3xl" };

export function Modal({ title, subtitle, onClose, children, footer, testId, size = "default" }) {
  const panel = useRef(null);

  useEffect(() => {
    // Where focus came from, so it can go back there. Without this, closing a
    // dialog drops focus onto <body> and a keyboard user restarts from the top
    // of the page every time.
    const opener = document.activeElement;

    const focusable = () =>
      Array.from(
        panel.current?.querySelectorAll(
          "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
        ) || []
      ).filter((el) => el.offsetParent !== null);

    const onKey = (e) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      // Trap Tab inside the dialog. `aria-modal` tells a screen reader the rest
      // of the page is inert; it does not tell the browser, so without this Tab
      // walks straight out into the page behind the scrim and the user is
      // editing a form they can no longer see.
      if (e.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !panel.current?.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    // The page behind must not scroll under the scrim.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    focusable()[0]?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
      // Only if the opener is still in the document — a dialog opened from a row
      // that the save then removed has nothing to return to.
      if (opener instanceof HTMLElement && document.contains(opener)) opener.focus();
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:items-center"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-testid={testId}
        className={`flex max-h-[calc(100vh-2rem)] w-full flex-col rounded-xl border border-border bg-panel shadow-2xl ${MODAL_WIDTHS[size] || MODAL_WIDTHS.default}`}
      >
        <header className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div>
            <h2 className="text-base font-semibold">{title}</h2>
            {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
          </div>
          <Button variant="ghost" onClick={onClose} aria-label="Close" data-testid={testId ? `${testId}-close` : undefined}>
            ✕
          </Button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <footer className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

/** Collapsed assumptions / methodology block. */
export const Disclosure = ({ summary, children, testId }) => (
  <details data-testid={testId} className="mt-4 rounded-lg border border-border bg-panel-2 px-4 py-2">
    <summary className="cursor-pointer py-1 text-sm font-medium text-muted">{summary}</summary>
    <div className="pt-2 pb-1 text-sm text-muted">{children}</div>
  </details>
);

export const PageHeader = ({ title, subtitle, actions }) => (
  <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
    <div>
      <h1 className="text-xl font-semibold">{title}</h1>
      {subtitle && <p className="mt-1 max-w-prose text-sm text-muted">{subtitle}</p>}
    </div>
    {actions}
  </div>
);

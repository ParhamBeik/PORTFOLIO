// Shared primitives. Pages compose these and write no bespoke panel, loading or
// error markup — that duplication is what made the old frontend inconsistent.
//
// Every element that a test or a screen reader needs to find takes a `testId`,
// rendered as `data-testid`. Naming convention: "<page>-<thing>", lowercase and
// hyphenated, e.g. "dashboard-total", "optimal-window-tabs".

const tone = {
  neutral: "text-text",
  muted: "text-muted",
  good: "text-[var(--c-good)]",
  warn: "text-[var(--c-warn)]",
  serious: "text-[var(--c-serious)]",
  critical: "text-[var(--c-critical)]",
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

export function StatTile({ label, value, sub, valueTone = "neutral", testId }) {
  return (
    <div data-testid={testId} className="rounded-lg border border-border bg-panel-2 px-4 py-3">
      <div className="text-xs font-medium tracking-wide text-muted uppercase">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone[valueTone]}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
  );
}

/** Status wears an icon-free but always-labelled chip — never hue alone. */
export function Badge({ children, variant = "neutral", title, testId }) {
  const styles = {
    neutral: "border-border bg-panel-2 text-muted",
    good: "border-[var(--c-good)]/40 bg-[var(--c-good)]/10 text-[var(--c-good)]",
    warn: "border-[var(--c-warn)]/40 bg-[var(--c-warn)]/10 text-[var(--c-warn)]",
    serious: "border-[var(--c-serious)]/40 bg-[var(--c-serious)]/10 text-[var(--c-serious)]",
    critical: "border-[var(--c-critical)]/40 bg-[var(--c-critical)]/10 text-[var(--c-critical)]",
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
  const styles = {
    primary: "bg-accent text-white hover:opacity-90 border-transparent",
    ghost: "bg-panel-2 text-text hover:bg-border border-border",
    danger: "bg-transparent text-[var(--c-critical)] hover:bg-[var(--c-critical)]/10 border-transparent",
    link: "bg-transparent text-accent underline underline-offset-2 border-transparent px-1 py-0",
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
            value === o.value ? "bg-accent text-white" : "text-muted hover:text-text"
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
 */
export function Table({ columns, rows, rowKey, empty = "No rows.", testId, caption }) {
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
              className="border-b border-border/60 last:border-0 hover:bg-panel-2"
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

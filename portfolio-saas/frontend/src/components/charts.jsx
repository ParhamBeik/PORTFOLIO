import { useEffect, useMemo, useRef, useState } from "react";
// Modular build, NOT `import * as echarts from "echarts"` -- the barrel import
// pulls every chart type and ships a 1,049 kB chunk (347 kB gzip). Registering
// only what this file draws cuts that by roughly 4x. A new chart type needs its
// module added to the `registerEcharts()` call below or it silently renders
// blank. Aliased on import because echarts names it `use`, which every React
// linter reads as a hook called at module scope.
import { graphic, init, use as registerEcharts } from "echarts/core";
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useT } from "../i18n.js";
import { compactAxisDigits, date, dateTime, dateTick, money, moneyCompact, pct, toman, tomanCompact, trendAxisTick } from "../format.js";

registerEcharts([
  BarChart, HeatmapChart, LineChart, PieChart, ScatterChart,
  GridComponent, LegendComponent, MarkLineComponent, TooltipComponent, VisualMapComponent,
  CanvasRenderer,
]);

// Themed ECharts wrappers. Pages never import echarts and never write a hex —
// the old code scattered #4c9aff/#3fb950/#f85149 across three files, so a theme
// change meant a grep.
//
// Colors come from the validated categorical palette in index.css. Those tokens
// are CSS custom properties, and the exports below stay in `var(--c-*)` form
// because pages spend them in style attributes, where var() resolves natively.
//
// ECharts cannot: it parses colors to compute hover/emphasis shades, and
// `var(--c-s1)` is not parseable, so a canvas would render it black. Every
// component therefore resolves tokens to hex through `useChartTokens`, which
// re-reads them when the OS colour scheme flips. That listener is what replaces
// the free re-theming SVG gave us for nothing.

export const SERIES = ["var(--c-s1)", "var(--c-s2)", "var(--c-s3)", "var(--c-s4)",
  "var(--c-s5)", "var(--c-s6)", "var(--c-s7)", "var(--c-s8)"];

export const STATUS_COLOR = {
  good: "var(--c-good)",
  warn: "var(--c-warn)",
  serious: "var(--c-serious)",
  critical: "var(--c-critical)",
};

const COVERAGE_COLORS = {
  complete: "var(--c-good)",
  fresh: "var(--c-good)",
  partial: "var(--c-warn)",
  refresh_due: "var(--c-warn)",
  awaiting_data: "var(--c-warn)",
  stale: "var(--c-warn)",
  failed: "var(--c-critical)",
  missing: "var(--c-critical)",
  not_tried: "var(--c-muted)",
  unfetchable: "var(--c-muted)",
  suspended: "var(--c-muted)",
  manual: "var(--c-s4)",
  formula: "var(--c-s5)",
  no_source: "var(--c-axis)",
};

const VAR_PATTERN = /^var\((--[\w-]+)\)$/;

/** Resolve a `var(--token)` string to its computed hex; pass through real colors. */
function resolveColor(value, styles) {
  if (typeof value !== "string") return value;
  const match = value.trim().match(VAR_PATTERN);
  if (!match) return value;
  return styles.getPropertyValue(match[1]).trim() || value;
}

/**
 * Palette resolved to hex, re-resolved when the OS colour scheme changes.
 *
 * index.css ships DIFFERENT steps per mode rather than flipping one set, so a
 * cached dark hex would be wrong in light mode, not merely dimmer.
 */
export function useChartTokens() {
  const read = () => {
    if (typeof window === "undefined") return null;
    const styles = getComputedStyle(document.documentElement);
    return {
      series: SERIES.map((token) => resolveColor(token, styles)),
      good: resolveColor("var(--c-good)", styles),
      warn: resolveColor("var(--c-warn)", styles),
      serious: resolveColor("var(--c-serious)", styles),
      critical: resolveColor("var(--c-critical)", styles),
      grid: resolveColor("var(--c-grid)", styles),
      axis: resolveColor("var(--c-axis)", styles),
      muted: resolveColor("var(--c-muted)", styles),
      text: resolveColor("var(--c-text)", styles),
      surface: resolveColor("var(--c-panel)", styles),
      border: resolveColor("var(--c-border)", styles),
      resolve: (value) => resolveColor(value, styles),
    };
  };

  const [tokens, setTokens] = useState(read);

  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => setTokens(read());
    query.addEventListener("change", onChange);
    // Custom properties are only readable after the stylesheet applies, which
    // can land after first paint on a cold load.
    onChange();
    return () => query.removeEventListener("change", onChange);
  }, []);

  return tokens;
}

/**
 * Minimal React binding for ECharts: init, keep in sync, resize, dispose.
 *
 * echarts-for-react would do this too, but it is ~40 lines of lifecycle for a
 * dependency that has to track React majors. `notMerge` is on because several
 * charts here change their series COUNT between renders, and a merged update
 * leaves the departed series on screen.
 */
function EChart({ option, height, label, testId, className = "w-full", onReady }) {
  const tr = useT();
  const host = useRef(null);
  const chart = useRef(null);
  // The newest option, readable from the mount effect. A chart that mounts late
  // has missed every `setOption` that already ran, so it has to ask for the
  // current one rather than wait for the next change -- otherwise a deferred
  // chart renders empty until something upstream happens to re-render.
  const latestOption = useRef(option);
  latestOption.current = option;
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;

  useEffect(() => {
    const el = host.current;
    if (!el) return undefined;
    let resizeObserver;

    // Charts below the fold used to pay their full init cost during first paint,
    // for a canvas nobody could see: on the optimizer page that was 100ms of
    // blocking time before anything had been scrolled to. Init when the
    // container is about to enter the viewport instead. `rootMargin` means it is
    // ready slightly BEFORE it becomes visible, so scrolling never reveals an
    // empty box -- the point is to move the work off the critical path, not to
    // trade it for a visible gap.
    const mount = () => {
      if (chart.current) return;
      chart.current = init(el);
      if (latestOption.current) chart.current.setOption(latestOption.current, true);
      onReadyRef.current?.(chart.current);
      resizeObserver = new ResizeObserver(() => chart.current?.resize());
      resizeObserver.observe(el);
    };

    let intersectionObserver;
    if (typeof IntersectionObserver === "function") {
      intersectionObserver = new IntersectionObserver(
        (entries) => {
          if (entries.some((entry) => entry.isIntersecting)) {
            intersectionObserver.disconnect();
            mount();
          }
        },
        { rootMargin: "200px" },
      );
      intersectionObserver.observe(el);
    } else {
      // No IntersectionObserver (old browser, or a test DOM): mount eagerly.
      // Deferring with no signal to un-defer on would leave the chart blank
      // forever, which is far worse than the blocking time this saves.
      mount();
    }

    return () => {
      intersectionObserver?.disconnect();
      resizeObserver?.disconnect();
      chart.current?.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    if (chart.current && option) chart.current.setOption(option, true);
  }, [option]);

  return (
    <div
      ref={host}
      className={className}
      style={{ height }}
      role="img"
      aria-label={label ? tr(label) : undefined}
      data-testid={testId}
    />
  );
}

/** Y domain tightly around series values (~8% pad; never forced through 0). */
function moneyTrendDomain(dataMin, dataMax) {
  const min = Number(dataMin);
  const max = Number(dataMax);
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  const span = hi - lo;
  const pad = span > 0 ? span * 0.08 : Math.max(Math.abs(hi) * 0.02, 1);
  const paddedMin = lo - pad;
  return [paddedMin < 0 && lo >= 0 ? 0 : paddedMin, hi + pad];
}

/** Stacked-share Y domain: symmetric around 50% so the even-split line stays centered. */
function shareTrendDomain(data, seriesKeys) {
  if (!data?.length || !seriesKeys?.length || seriesKeys.length <= 1) return [0, 1];

  let reach = 0;
  for (const row of data) {
    let cum = 0;
    for (let i = 0; i < seriesKeys.length - 1; i += 1) {
      const v = Number(row[seriesKeys[i]]);
      if (Number.isFinite(v)) cum += v;
      reach = Math.max(reach, Math.abs(cum - 0.5));
    }
  }
  const pad = reach > 0 ? reach * 0.08 : 0;
  const radius = Math.min(0.5, Math.max(reach + pad, 0.02));
  return [0.5 - radius, 0.5 + radius];
}

/**
 * Snap a padded domain out to round bounds and return the tick interval.
 *
 * Without this the axis divides an arbitrary padded range into equal parts that
 * land on values like 2.75B and 2.84B, which `tomanCompact` then renders as
 * "2.8B" TWICE -- one axis, two identical labels, a real defect rather than a
 * cosmetic one. Snapping to a 1/2/5 x 10^n step makes every tick a round number,
 * so distinct ticks always get distinct labels.
 */
function niceAxis(min, max, splits = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
    return { min, max, interval: undefined };
  }
  const rough = (max - min) / splits;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
  return {
    min: Math.floor(min / step) * step,
    max: Math.ceil(max / step) * step,
    interval: step,
  };
}

function extent(rows, key = "y") {
  let lo = Infinity;
  let hi = -Infinity;
  for (const row of rows || []) {
    // `Number(null)` is 0 and 0 is finite, so a null point would drag the axis
    // floor to zero and flatten a series that lives up at 58 million.
    if (row[key] == null) continue;
    const v = Number(row[key]);
    if (!Number.isFinite(v)) continue;
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  return Number.isFinite(lo) ? [lo, hi] : [0, 1];
}

function seriesExtent(data, keys) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const row of data || []) {
    for (const key of keys) {
      const v = Number(row[key]);
      if (!Number.isFinite(v)) continue;
      lo = Math.min(lo, v);
      hi = Math.max(hi, v);
    }
  }
  return Number.isFinite(lo) ? [lo, hi] : [0, 1];
}

/** Shared axis/grid/tooltip chrome so every chart reads as one system. */
function chrome(t) {
  return {
    textStyle: { fontFamily: "inherit" },
    grid: { top: 16, right: 16, bottom: 8, left: 8, containLabel: true },
    tooltipBase: {
      backgroundColor: t.surface,
      borderColor: t.border,
      borderWidth: 1,
      padding: [8, 10],
      textStyle: { color: t.text, fontSize: 12 },
      // Capped, so a long portfolio name cannot make the box wider than a
      // phone: `confine` keeps a box inside the chart only if it fits there.
      extraCssText: "border-radius:8px;max-width:min(320px,calc(100vw - 32px));white-space:normal;",
      // Keep the tooltip inside the chart's own box. Unconfined, ECharts places
      // it beside the pointer, and a tap near the edge of a phone screen put it
      // 22px off the left edge with the asset name cut off.
      confine: true,
    },
    categoryAxis: {
      axisLine: { lineStyle: { color: t.axis } },
      axisTick: { show: false },
      axisLabel: { color: t.muted, fontSize: 11 },
    },
    valueAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: t.muted, fontSize: 11 },
      splitLine: { lineStyle: { color: t.grid } },
    },
    legend: (t2 = t) => ({
      bottom: 0,
      icon: "circle",
      itemWidth: 8,
      itemHeight: 8,
      textStyle: { color: t2.muted, fontSize: 12 },
      // A portfolio name is user-typed: one long enough to outrun the chart
      // would otherwise run off both edges with nothing to reach the rest.
      type: "scroll",
      pageTextStyle: { color: t2.muted },
      formatter: (name) => (name.length > 22 ? `${name.slice(0, 21)}…` : name),
    }),
  };
}

/** Tooltip rows: a colored dot carries identity, the text stays in ink tokens. */
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/**
 * Escape text bound for a tooltip.
 *
 * ECharts renders a formatter's returned string as HTML, and the labels these
 * tooltips print are user-typed: a holding nickname is free text. Unescaped, a
 * nickname containing `<` is swallowed by the parser (the row renders blank),
 * and one shaped like a tag executes in the owner's own session. Every name
 * reaching a tooltip goes through the two helpers below, so escaping here
 * covers all of them.
 *
 * GHSA-fgmj-fm8m-jvvx (ECharts XSS, all versions < 6.1.0) was carried here for
 * a while on the argument that it was unreachable — CanvasRenderer is the only
 * renderer imported, so the library's own text paths paint to a canvas rather
 * than to DOM, leaving the tooltip as the single HTML surface. That reasoning
 * held, but it was load-bearing and undefended: nothing in CI checked either
 * half of it. The project is now on 6.1.0 and the advisory is simply fixed.
 *
 * The escaping below is still required regardless of version, and is now pinned
 * by `charts.spec.jsx`, which mounts a holding named `<img src=x onerror=...>`
 * and asserts no element is created. Adding a chart that leans on ECharts'
 * default tooltip formatter, rather than supplying one that calls `esc()`,
 * still reintroduces the bug this guards.
 */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

function tipRows(params, t, fmt) {
  const list = Array.isArray(params) ? params : [params];
  return list
    .map((p) => {
      const dot =
        `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;` +
        `background:${p.color};margin-right:6px"></span>`;
      const value = fmt(p.value?.[1] ?? p.value, p);
      // ECharts can name an unnamed series "series0" or "series 0"; pie
      // slices carry the real label on the datum (`name` = Stock / Gold / …).
      // On an axis-triggered tooltip `name` is the CATEGORY -- the date -- so
      // every line in a multi-series chart was labelled "2026-10-03" instead
      // of the portfolio or asset it is. There the series name comes first.
      const genericSeries = /^series\s*\d+$/i.test(p.seriesName || "");
      const series = !genericSeries ? p.seriesName : "";
      const label = (p.axisValue !== undefined ? series || p.name : p.name || series) || "—";
      return `<div style="display:flex;gap:8px;align-items:center;justify-content:space-between">` +
        `<span style="color:${t.text};min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${dot}${esc(label)}</span>` +
        `<span style="color:${t.text};flex:none;font-variant-numeric:tabular-nums">${value}</span></div>`;
    })
    .join("");
}

function header(label, t) {
  return `<div style="color:${t.muted};margin-bottom:4px">${esc(label)}</div>`;
}

/**
 * Net-worth over time. One series, so no legend — the panel title names it.
 * `data` is [{ x: ISO date, y: number }].
 */
/**
 * `basis` says which currency `data.y` is already denominated in. The server
 * converts the points and used to leave the label behind, so a portfolio
 * switched to USD drew dollars against a Toman axis under a "T" tooltip.
 */
export function AreaTrend({ data, height = 260, label = "Portfolio value over time", longTicks, basis = "nominal_toman" }) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    const [lo, hi] = extent(data);
    const { min, max, interval } = niceAxis(...moneyTrendDomain(lo, hi));
    return {
      ...c,
      grid: { ...c.grid, top: 12, right: 12 },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "line", lineStyle: { color: t.axis, type: "dashed" } },
        formatter: (p) => header(date(p[0].axisValue), t) + tipRows(p, t, (v) => money(v, basis)),
      },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: (data || []).map((d) => d.x),
        ...c.categoryAxis,
        axisLabel: { ...c.categoryAxis.axisLabel, hideOverlap: true,
          formatter: (v) => dateTick(v, longTicks) },
      },
      yAxis: {
        type: "value", min, max, interval, ...c.valueAxis,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => moneyCompact(v, basis, compactAxisDigits(max, interval)) },
      },
      series: [{
        type: "line",
        name: "Value",
        smooth: true,
        showSymbol: false,
        symbolSize: 8,
        // A day with no value is a break in the line, never a dive to zero.
        data: (data || []).map((d) => (d.y == null ? null : Number(d.y))),
        lineStyle: { width: 2, color: t.series[0] },
        itemStyle: { color: t.series[0], borderColor: t.surface, borderWidth: 2 },
        areaStyle: {
          color: new graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: t.series[0] + "59" },
            { offset: 1, color: t.series[0] + "00" },
          ]),
        },
      }],
      animation: false,
    };
  }, [data, t, longTicks, basis]);

  return <EChart option={option} height={height} label={label} />;
}

/**
 * Several portfolio net-worth series on one axis.
 * `series` = [{ key, name }], `data` = [{ x, [key]: number }].
 */
/**
 * `formatValue` / `formatAxis` default to Toman because most callers plot money.
 * Rebased-to-100 series are not money, and printing "۱۰۰ ت" on an index axis
 * says the portfolio is worth a hundred Tomans.
 */
export function MultiLineTrend({
  series,
  data,
  height = 280,
  label = "Portfolio values over time",
  longTicks,
  formatValue = toman,
  formatAxis = tomanCompact,
}) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    const keys = (series || []).map((s) => s.key);
    const [lo, hi] = seriesExtent(data, keys);
    const { min, max, interval } = niceAxis(...moneyTrendDomain(lo, hi));
    return {
      ...c,
      grid: { ...c.grid, bottom: 28 },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "line", lineStyle: { color: t.axis, type: "dashed" } },
        formatter: (p) => header(date(p[0].axisValue), t) + tipRows(p, t, (v) => formatValue(v)),
      },
      legend: c.legend(),
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: (data || []).map((d) => d.x),
        ...c.categoryAxis,
        axisLabel: { ...c.categoryAxis.axisLabel, hideOverlap: true,
          formatter: (v) => dateTick(v, longTicks) },
      },
      yAxis: {
        type: "value", min, max, interval, ...c.valueAxis,
        // The default compact formatter gets enough decimals to keep ticks
        // distinct; a caller's own formatter is used as given.
        axisLabel: {
          ...c.valueAxis.axisLabel,
          formatter: formatAxis === tomanCompact ? (v) => tomanCompact(v, compactAxisDigits(max, interval)) : formatAxis,
        },
      },
      series: (series || []).map((s, i) => ({
        type: "line",
        name: s.name,
        smooth: true,
        showSymbol: false,
        symbolSize: 8,
        data: (data || []).map((d) => {
          const v = Number(d[s.key]);
          return Number.isFinite(v) ? v : null;
        }),
        lineStyle: { width: 2, color: t.series[i % t.series.length] },
        itemStyle: { color: t.series[i % t.series.length], borderColor: t.surface, borderWidth: 2 },
      })),
      animation: false,
    };
  }, [series, data, t, longTicks, formatValue, formatAxis]);

  return <EChart option={option} height={height} label={label} />;
}

/**
 * Stacked share-of-total (fractions summing to ~1). `series` / `data` same shape as MultiLineTrend.
 */
export function StackedShareTrend({
  series,
  data,
  height = 280,
  label = "Share of combined portfolio over time",
  longTicks,
}) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    const keys = (series || []).map((s) => s.key);
    const [min, max] = shareTrendDomain(data, keys);
    const decimals = max - min < 0.2 ? 1 : 0;
    return {
      ...c,
      grid: { ...c.grid, bottom: 28 },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "line", lineStyle: { color: t.axis, type: "dashed" } },
        formatter: (p) => header(date(p[0].axisValue), t) + tipRows(p, t, (v) => pct(v)),
      },
      legend: c.legend(),
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: (data || []).map((d) => d.x),
        ...c.categoryAxis,
        axisLabel: { ...c.categoryAxis.axisLabel, hideOverlap: true,
          formatter: (v) => dateTick(v, longTicks) },
      },
      yAxis: {
        type: "value", min, max, ...c.valueAxis,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, decimals) },
      },
      series: (series || []).map((s, i) => ({
        type: "line",
        name: s.name,
        stack: "share",
        smooth: true,
        showSymbol: false,
        symbolSize: 8,
        data: (data || []).map((d) => {
          const v = Number(d[s.key]);
          return Number.isFinite(v) ? v : null;
        }),
        lineStyle: { width: 2, color: t.series[i % t.series.length] },
        itemStyle: { color: t.series[i % t.series.length] },
        areaStyle: { color: t.series[i % t.series.length], opacity: 0.55 },
        // The even-split reference: a 50/50 book tracks this line exactly.
        markLine: i === 0 ? {
          silent: true,
          symbol: "none",
          label: { show: false },
          lineStyle: { color: t.muted, type: "dashed", width: 1 },
          data: [{ yAxis: 0.5 }],
        } : undefined,
      })),
      animation: false,
    };
  }, [series, data, t, longTicks]);

  return <EChart option={option} height={height} label={label} />;
}

/**
 * Composition by category. `data` is [{ name, value }].
 *
 * `valueFormat` renders the raw magnitude in the centre readout. It defaults to
 * Toman because most callers pass money, but a caller passing weights must pass
 * `pct` — otherwise a 0.35 weight reads as "0 T".
 *
 * Hovering or tapping a slice OR its legend row selects it and the ring's centre
 * shows that slice's name, value and share. A 1% slice is a few pixels wide and
 * cannot be hit with a finger; the legend row is a full-width target.
 *
 * The legend carries name + share as text: that is the relief for the three
 * light-mode palette slots that sit under 3:1 against white, so identity is
 * never colour-alone.
 */
export function Donut({ data, height = 260, label = "Allocation breakdown", valueFormat = toman, testId, totalLabel = "Total" }) {
  const t = useChartTokens();
  // Class names ("Gold", "Cash") have Persian entries; a user's own portfolio
  // or holding name has none and comes back unchanged.
  const tr = useT();
  const chart = useRef(null);
  const [active, setActive] = useState(null);
  const rows = data || [];
  const total = rows.reduce((s, d) => s + Number(d.value || 0), 0) || 1;
  const option = useMemo(() => {
    if (!t) return null;
    return {
      // No floating tooltip: the selection is read out in the ring's centre,
      // which cannot leave the screen and works the same for a tap as a hover.
      tooltip: { show: false },
      series: [{
        type: "pie",
        radius: ["62%", "88%"],
        avoidLabelOverlap: false,
        label: { show: false },
        labelLine: { show: false },
        emphasis: { scale: true, scaleSize: 4, focus: "self" },
        data: (data || []).map((d, i) => ({
          name: d.name,
          value: Number(d.value || 0),
          itemStyle: {
            color: t.series[(d.slot ?? i) % t.series.length],
            // 2px surface ring separates adjacent slices.
            borderColor: t.surface,
            borderWidth: 2,
          },
        })),
      }],
      animation: false,
    };
  }, [data, t]);

  const select = (i) => {
    const c = chart.current;
    setActive(i);
    if (!c) return;
    c.dispatchAction({ type: "downplay", seriesIndex: 0 });
    if (i != null) c.dispatchAction({ type: "highlight", seriesIndex: 0, dataIndex: i });
  };
  const onReady = (c) => {
    chart.current = c;
    c.on("mouseover", (p) => select(p.dataIndex));
    c.on("click", (p) => select(p.dataIndex));
    c.on("globalout", () => select(null));
  };

  const shown = active != null ? rows[active] : null;
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-4" data-testid={testId}>
      <div className="relative min-w-56 flex-1">
        <EChart option={option} height={height} label={label} onReady={onReady} />
        <div
          className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center px-[22%] text-center"
          aria-live="polite"
          data-testid={testId ? `${testId}-readout` : undefined}
        >
          <span className="max-w-full truncate text-xs text-muted">{shown ? tr(shown.name) : tr(totalLabel)}</span>
          <span className="tabular max-w-full truncate text-sm font-semibold text-text">
            {valueFormat(shown ? Number(shown.value) : rows.reduce((s, d) => s + Number(d.value || 0), 0))}
          </span>
          {shown && <span className="tabular text-xs text-muted">{pct(Number(shown.value) / total)}</span>}
        </div>
      </div>
      <ul className="min-w-48 flex-1 text-sm">
        {rows.map((d, i) => (
          <li key={d.name}>
            <button
              type="button"
              // Hover is mouse-only: a tap fires a synthetic mouseenter AND a
              // click, so a toggling click undid the selection it had just made.
              onPointerEnter={(e) => e.pointerType === "mouse" && select(i)}
              onPointerLeave={(e) => e.pointerType === "mouse" && select(null)}
              onFocus={() => select(i)}
              onClick={() => select(i)}
              aria-pressed={active === i}
              className={`flex min-h-10 w-full items-center gap-2 rounded-md px-2 text-start ${active === i ? "bg-panel-2" : ""}`}
            >
              <span
                aria-hidden="true"
                className="size-2.5 shrink-0 rounded-full"
                style={{ background: SERIES[(d.slot ?? i) % SERIES.length] }}
              />
              <span className="min-w-0 flex-1 truncate text-text">{tr(d.name)}</span>
              <span className="tabular text-muted">{pct(Number(d.value) / total)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// `Number(null)` is 0, so mapping every point through it turned "we did not
// measure this hour" into "the table held zero rows" -- a spike to the floor and
// back on 4.5% of the ops growth points, on tables that only ever grow. A
// missing measurement stays null and echarts breaks the line there instead.
function trimLeadingZeroPoints(data) {
  const rows = (data || []).map((p) => ({
    x: p.x,
    y: p.y == null || p.y === "" ? null : Number(p.y),
  }));
  let start = 0;
  while (start < rows.length - 1 && (rows[start].y === 0 || rows[start].y === null)) start += 1;
  return rows.slice(start);
}

function seriesSpanMs(rows) {
  const times = rows.map((r) => new Date(r.x).getTime()).filter((t) => !Number.isNaN(t));
  if (times.length < 2) return 0;
  return Math.max(...times) - Math.min(...times);
}

export function CountTrend({ data, height = 180, label = "Count over time", color = "var(--c-s1)" }) {
  const t = useChartTokens();
  const rows = trimLeadingZeroPoints(data);
  const spanMs = seriesSpanMs(rows);
  const shortSpan = spanMs > 0 && spanMs <= 2 * 86400000;

  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    const hex = t.resolve(color);
    const [lo, hi] = extent(rows);
    const { min, max, interval } = niceAxis(...moneyTrendDomain(lo, hi));
    return {
      ...c,
      grid: { ...c.grid, top: 12, bottom: shortSpan ? 16 : 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "line", lineStyle: { color: t.axis, type: "dashed" } },
        formatter: (p) =>
          header(shortSpan ? dateTime(p[0].axisValue) : date(p[0].axisValue), t) +
          tipRows(p, t, (v) => numFmt(v)),
      },
      xAxis: {
        type: "category", boundaryGap: false,
        data: rows.map((d) => d.x),
        ...c.categoryAxis,
        axisLabel: { ...c.categoryAxis.axisLabel, hideOverlap: true,
          formatter: (v) => trendAxisTick(v, spanMs) },
      },
      yAxis: {
        type: "value", min, max, interval, ...c.valueAxis,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: numFmt },
      },
      series: [{
        type: "line", name: label, smooth: true, showSymbol: false, symbolSize: 8,
        data: rows.map((d) => d.y),
        // Leave the gap visible. Bridging it would draw a straight line across
        // an interval nobody measured, which reads as data rather than absence.
        connectNulls: false,
        lineStyle: { width: 2, color: hex },
        itemStyle: { color: hex, borderColor: t.surface, borderWidth: 2 },
        areaStyle: { color: hex, opacity: 0.15 },
      }],
      animation: false,
    };
  }, [rows, t, color, spanMs, shortSpan, label]);

  return <EChart option={option} height={height} label={label} />;
}

/** Tiny per-day bars with no axes -- "is this still moving?" at a glance.
 *  A null day (not measured) draws no bar rather than a zero one. */
export function SparkBars({ data, height = 44, label = "Per day", color = "var(--c-s1)", testId }) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    const hex = t.resolve(color);
    return {
      ...c,
      grid: { left: 0, right: 0, top: 2, bottom: 0 },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        formatter: (p) => header(date(p[0].axisValue), t) + tipRows(p, t, (v) => numFmt(v)),
      },
      xAxis: { type: "category", data: data.map((d) => d.x), show: false },
      yAxis: { type: "value", show: false, min: 0 },
      series: [{ type: "bar", name: label, data: data.map((d) => d.y), itemStyle: { color: hex, borderRadius: 1 }, barCategoryGap: "20%" }],
      animation: false,
    };
  }, [data, t, color, label]);
  return <EChart option={option} height={height} label={label} testId={testId} />;
}

function numFmt(v) {
  // Same `Number(null) === 0` trap as `extent`: without this the tooltip on an
  // unmeasured hour reads "0" instead of "not measured".
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}

/** Stacked counts per category — e.g. complete / partial / failed / not_tried per endpoint. */
export function StackedStatusBar({
  data,
  series,
  height = 320,
  label = "Status breakdown",
  testId,
}) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    // Bars are normalized to 100% (the "expand" stack offset),
    // but the tooltip must still report the RAW count -- a share alone cannot
    // tell "3 of 4 failed" from "300 of 400".
    const totals = (data || []).map((row) =>
      (series || []).reduce((sum, s) => sum + (Number(row[s.key]) || 0), 0) || 1
    );
    return {
      ...c,
      grid: { ...c.grid, bottom: 34 },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "shadow" },
        formatter: (p) =>
          header(p[0].axisValue, t) +
          tipRows(p, t, (_v, item) => String(Math.round(item.data.raw))),
      },
      legend: c.legend(),
      xAxis: {
        type: "category",
        data: (data || []).map((d) => d.name),
        ...c.categoryAxis,
        axisLabel: { ...c.categoryAxis.axisLabel, interval: 0, rotate: 20 },
      },
      yAxis: {
        type: "value", max: 1, ...c.valueAxis,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => `${Math.round(v * 100)}%` },
      },
      series: (series || []).map((s) => ({
        type: "bar",
        name: s.name,
        stack: "status",
        data: (data || []).map((row, i) => ({
          value: (Number(row[s.key]) || 0) / totals[i],
          raw: Number(row[s.key]) || 0,
        })),
        itemStyle: {
          color: t.resolve(s.color || COVERAGE_COLORS[s.key] || SERIES[0]),
          // 2px surface gap between stacked segments, per the mark spec.
          borderColor: t.surface,
          borderWidth: 1,
        },
      })),
      animation: false,
    };
  }, [data, series, t]);

  return (
    <div data-testid={testId}>
      <EChart option={option} height={height} label={label} />
    </div>
  );
}

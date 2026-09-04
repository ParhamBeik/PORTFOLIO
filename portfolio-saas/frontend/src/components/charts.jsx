import { useEffect, useMemo, useRef, useState } from "react";
// Modular build, NOT `import * as echarts from "echarts"` -- the barrel import
// pulls every chart type and ships a 1,049 kB chunk (347 kB gzip). Registering
// only what this file draws cuts that by roughly 4x. A new chart type needs its
// module added to the `use()` call below or it silently renders blank.
import { graphic, init, use } from "echarts/core";
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { date, dateTime, dateTick, money, moneyCompact, pct, toman, tomanCompact, trendAxisTick } from "../format.js";

use([
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

export const COVERAGE_COLORS = {
  complete: "var(--c-good)",
  fresh: "var(--c-good)",
  partial: "var(--c-warn)",
  stale: "var(--c-warn)",
  failed: "var(--c-critical)",
  missing: "var(--c-critical)",
  not_tried: "var(--c-muted)",
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
function EChart({ option, height, label, testId, className = "w-full" }) {
  const host = useRef(null);
  const chart = useRef(null);
  // The newest option, readable from the mount effect. A chart that mounts late
  // has missed every `setOption` that already ran, so it has to ask for the
  // current one rather than wait for the next change -- otherwise a deferred
  // chart renders empty until something upstream happens to re-render.
  const latestOption = useRef(option);
  latestOption.current = option;

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
      aria-label={label}
      data-testid={testId}
    />
  );
}

/** Y domain tightly around series values (~8% pad; never forced through 0). */
export function moneyTrendDomain(dataMin, dataMax) {
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
export function shareTrendDomain(data, seriesKeys) {
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
      extraCssText: "border-radius:8px;",
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
 * This is also what contains GHSA-fgmj-fm8m-jvvx (ECharts XSS, all versions
 * < 6.1.0, which is what this project pins). Two properties make it
 * unreachable, and BOTH must hold — check them before relaxing either:
 *
 *   1. `CanvasRenderer` is the only renderer imported. Axis labels, legends and
 *      series names are painted to a canvas, never inserted as DOM, so the
 *      library's own text paths cannot inject markup.
 *   2. The tooltip is therefore the single HTML surface, and every tooltip that
 *      renders supplies a custom `formatter` running its labels through `esc()`.
 *      The only tooltips without one are `{ show: false }`.
 *
 * Switching to the SVG renderer, or adding a chart that leans on ECharts'
 * default tooltip formatter, re-opens it. Upgrading to 6.x is the durable fix.
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
      const genericSeries = /^series\s*\d+$/i.test(p.seriesName || "");
      const label = p.name || (!genericSeries ? p.seriesName : "") || "—";
      return `<div style="display:flex;gap:8px;align-items:center;justify-content:space-between">` +
        `<span style="color:${t.text}">${dot}${esc(label)}</span>` +
        `<span style="color:${t.text};font-variant-numeric:tabular-nums">${value}</span></div>`;
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
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => moneyCompact(v, basis) },
      },
      series: [{
        type: "line",
        name: "Value",
        smooth: true,
        showSymbol: false,
        symbolSize: 8,
        data: (data || []).map((d) => Number(d.y)),
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
        axisLabel: { ...c.valueAxis.axisLabel, formatter: formatAxis },
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
 * Two-series comparison, e.g. current vs target weight.
 * `data` is [{ name, a, b }]; `labels` is [aLabel, bLabel]. Values are fractions.
 */
export function GroupedBar({ data, labels, height = 300, label = "Comparison", testId }) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    return {
      ...c,
      grid: { ...c.grid, bottom: 32 },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "shadow" },
        formatter: (p) => header(p[0].axisValue, t) + tipRows(p, t, (v) => pct(v)),
      },
      legend: c.legend(),
      xAxis: {
        type: "category",
        data: (data || []).map((d) => d.name),
        ...c.categoryAxis,
        axisLabel: { ...c.categoryAxis.axisLabel, interval: 0, rotate: 25, hideOverlap: false },
      },
      yAxis: {
        type: "value", ...c.valueAxis,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, 0) },
      },
      series: [0, 1].map((i) => ({
        type: "bar",
        name: labels[i],
        // 2px surface gap between adjacent bars, per the mark spec.
        barGap: "8%",
        data: (data || []).map((d) => Number(i === 0 ? d.a : d.b)),
        itemStyle: {
          color: t.series[i],
          borderRadius: [4, 4, 0, 0],
        },
      })),
      animation: false,
    };
  }, [data, labels, t]);

  return <EChart option={option} height={height} label={label} testId={testId} />;
}

/**
 * Composition by category. `data` is [{ name, value }].
 *
 * `valueFormat` renders the raw magnitude in the tooltip. It defaults to Toman
 * because most callers pass money, but a caller passing weights must pass `pct`
 * — otherwise a 0.35 weight reads as "0 T".
 *
 * The legend carries name + share as text: that is the relief for the three
 * light-mode palette slots that sit under 3:1 against white, so identity is
 * never colour-alone.
 */
export function Donut({ data, height = 260, label = "Allocation breakdown", valueFormat = toman, testId }) {
  const t = useChartTokens();
  const total = (data || []).reduce((s, d) => s + Number(d.value || 0), 0) || 1;
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    return {
      tooltip: {
        trigger: "item",
        ...c.tooltipBase,
        formatter: (p) =>
          tipRows(p, t, () => `${valueFormat(p.value)} · ${pct(p.value / total)}`),
      },
      series: [{
        type: "pie",
        radius: ["58%", "86%"],
        avoidLabelOverlap: false,
        label: { show: false },
        labelLine: { show: false },
        data: (data || []).map((d, i) => ({
          name: d.name,
          value: Number(d.value || 0),
          itemStyle: {
            color: t.series[i % t.series.length],
            // 2px surface ring separates adjacent slices.
            borderColor: t.surface,
            borderWidth: 2,
          },
        })),
      }],
      animation: false,
    };
  }, [data, t, total, valueFormat]);

  return (
    <div className="flex flex-wrap items-center gap-6" data-testid={testId}>
      <EChart option={option} height={height} label={label} className="min-w-56 flex-1" />
      <ul className="min-w-48 flex-1 space-y-1.5 text-sm">
        {(data || []).map((d, i) => (
          <li key={d.name} className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="size-2.5 shrink-0 rounded-full"
              style={{ background: SERIES[i % SERIES.length] }}
            />
            <span className="flex-1 truncate text-text">{d.name}</span>
            <span className="tabular text-muted">{pct(Number(d.value) / total)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Risk/return space: the achievable cloud, the efficient frontier, and two
 * named reference points. The points use reserved status colours, not series
 * slots, and each is named in the caption the page renders beneath.
 *
 * All inputs are fractions; axes render them as percentages.
 */
export function RiskScatter({ frontier = [], cloud = [], points = [], height = 340, label = "Risk versus return" }) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t) return null;
    const c = chrome(t);
    const axisName = { color: t.muted, fontSize: 11 };
    return {
      ...c,
      grid: { ...c.grid, top: 16, right: 20, bottom: 36, left: 12 },
      tooltip: {
        trigger: "item",
        ...c.tooltipBase,
        formatter: (p) => {
          const [x, y] = p.value;
          return header(p.seriesName, t) +
            `<div style="color:${t.text}">Volatility ${pct(x)} · Return ${pct(y)}</div>`;
        },
      },
      xAxis: {
        type: "value", ...c.valueAxis, scale: true,
        name: "Annualized volatility", nameLocation: "middle", nameGap: 26, nameTextStyle: axisName,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, 0) },
      },
      yAxis: {
        type: "value", ...c.valueAxis, scale: true,
        name: "Annualized return", nameLocation: "middle", nameGap: 44, nameTextStyle: axisName,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, 0) },
      },
      series: [
        cloud.length > 0 && {
          type: "scatter", name: "Achievable", symbolSize: 8,
          data: cloud.map((d) => [d.x, d.y]),
          itemStyle: { color: t.muted, opacity: 0.22 },
        },
        {
          type: "scatter", name: "Efficient frontier", symbolSize: 8,
          data: (frontier || []).map((d) => [d.x, d.y]),
          itemStyle: { color: t.series[0] },
        },
        ...points.map((p) => ({
          type: "scatter", name: p.name, symbolSize: 14,
          data: [[p.x, p.y]],
          // 2px surface ring so a reference point stays legible on the cloud.
          itemStyle: { color: t.resolve(p.color), borderColor: t.surface, borderWidth: 2 },
          z: 10,
        })),
      ].filter(Boolean),
      animation: false,
    };
  }, [frontier, cloud, points, t]);

  return <EChart option={option} height={height} label={label} />;
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
    // Bars are normalized to 100% (recharts called this stackOffset="expand"),
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

/**
 * Share of money against share of RISK, one row per holding.
 *
 * A dumbbell rather than paired bars: the finding here is the GAP, and a
 * connecting segment encodes it as length directly instead of asking the eye to
 * difference two bar heights. Both endpoints stay visible because level matters
 * too — a 12-point gap on a 38% position is a different problem from the same
 * gap on a 2% position.
 *
 * Horizontal because the category labels are asset keys ("one_gram_coin"), which
 * a vertical axis would rotate to 25 degrees and clip.
 *
 * `rows` is the API's `diversification.concentration_gap`:
 * [{ key, weight_share, risk_share, gap }], already sorted worst-first.
 */
export function MoneyVsRisk({
  rows = [],
  height,
  label = "Share of money versus share of risk",
  coverage,
  valueFor,
  basis = "nominal_toman",
  testId,
}) {
  const t = useChartTokens();
  // Worst offender on top: ECharts category axes build upward, so reverse.
  const ordered = useMemo(() => [...rows].reverse(), [rows]);
  const option = useMemo(() => {
    if (!t || !ordered.length) return null;
    const c = chrome(t);
    // Named for the colour it is, not shadowing the `money` formatter imported
    // at the top of this file -- the tooltip below needs that formatter, because
    // the amounts come from the valuation payload the server has already
    // converted and printing them as Toman mislabels a dollar figure.
    const moneyColor = t.series[0];
    const risk = t.series[1];
    return {
      ...c,
      // Legend above the plot, not below: the value axis now carries a name in
      // the same place a bottom legend would sit, and the two collided.
      grid: { top: 34, right: 64, bottom: 34, left: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "shadow" },
        // Reads both numbers off the row, NOT off the hovered series. The shared
        // `tipRows` helper takes `p.value[1]`, which on these scatter points is
        // the category index -- so the tooltip reported a row's position in the
        // list as a percentage ("200%" for the third asset). Everything the
        // tooltip states is a real quantity from `rows`.
        formatter: (p) => {
          const row = ordered[p[0].dataIndex];
          const line = (color, text, value) =>
            `<div style="display:flex;gap:12px;align-items:center;justify-content:space-between">` +
            `<span style="color:${t.text}"><span style="display:inline-block;width:8px;height:8px;` +
            `border-radius:50%;background:${color};margin-right:6px"></span>${text}</span>` +
            `<span style="color:${t.text};font-variant-numeric:tabular-nums">${value}</span></div>`;
          const amount = valueFor?.(row.key);
          // The gap is a difference between two shares, so it is measured in
          // percentage POINTS. Calling it "16.1% more risk" would state a ratio
          // the number is not.
          const points = (Math.abs(row.gap) * 100).toFixed(1);
          const verdict =
            row.gap >= 0
              ? `Carries ${points} points more of the risk than of the money`
              : `Carries ${points} points less of the risk than of the money`;
          return (
            header(row.key, t) +
            line(moneyColor, "Share of your money", pct(row.weight_share)) +
            line(risk, "Share of your risk", pct(row.risk_share)) +
            `<div style="margin-top:4px;color:${t.muted}">` +
            verdict +
            (amount ? ` · ${money(amount, basis)}` : "") +
            `</div>`
          );
        },
      },
      legend: {
        ...c.legend(), bottom: undefined, top: 0, left: "center",
        data: ["Share of money", "Share of risk"],
      },
      xAxis: {
        type: "value", ...c.valueAxis,
        name: "Share of portfolio",
        nameLocation: "middle",
        nameGap: 26,
        nameTextStyle: { color: t.muted, fontSize: 11 },
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, 0) },
      },
      yAxis: {
        type: "category",
        data: ordered.map((r) => r.key),
        ...c.categoryAxis,
        splitLine: { show: false },
      },
      series: [
        {
          // The connector carries the gap. Drawn first so the dots sit on top.
          type: "bar",
          name: "Gap",
          silent: true,
          barWidth: 2,
          stack: "connector",
          itemStyle: { color: "transparent" },
          data: ordered.map((r) => Math.min(r.weight_share, r.risk_share)),
          legendHoverLink: false,
          tooltip: { show: false },
        },
        {
          type: "bar",
          name: "Gap",
          barWidth: 2,
          stack: "connector",
          itemStyle: { color: t.muted, opacity: 0.55 },
          data: ordered.map((r) => Math.abs(r.risk_share - r.weight_share)),
          tooltip: { show: false },
          // Direct-label the gap at the end of the row: the number people quote.
          label: {
            show: true,
            position: "right",
            distance: 12,
            color: t.muted,
            fontSize: 11,
            formatter: (p) => {
              const row = ordered[p.dataIndex];
              return `${row.gap >= 0 ? "+" : "−"}${pct(Math.abs(row.gap), 0)}`;
            },
          },
        },
        {
          type: "scatter", name: "Share of money", symbolSize: 11,
          data: ordered.map((r, i) => [r.weight_share, i]),
          itemStyle: { color: money, borderColor: t.surface, borderWidth: 2 },
          z: 5,
        },
        {
          type: "scatter", name: "Share of risk", symbolSize: 11,
          data: ordered.map((r, i) => [r.risk_share, i]),
          itemStyle: { color: risk, borderColor: t.surface, borderWidth: 2 },
          z: 5,
        },
      ],
      animation: false,
    };
  }, [ordered, t, valueFor, basis]);

  const rowHeight = 34;
  return (
    <EChart
      option={option}
      height={height || Math.max(180, ordered.length * rowHeight + 90)}
      label={coverage != null && coverage < 0.999 ? `${label} · ${pct(coverage)} of portfolio covered` : label}
      testId={testId}
    />
  );
}

/**
 * Correlation heatmap. `assets` are labels, `matrix` is the square payload from
 * /api/analytics/ .correlation.
 *
 * Diverging, because correlation has a meaningful zero: two hues with a NEUTRAL
 * grey midpoint, never a rainbow, and the scale is pinned to [-1, 1] so colour
 * means the same thing on every render. A per-render domain would make a book of
 * mildly-correlated assets look as alarming as one that moves in lockstep.
 */
export function CorrelationHeatmap({ assets = [], matrix = [], height, label = "Correlation between holdings", testId }) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t || !assets.length) return null;
    const c = chrome(t);
    const cells = [];
    for (let i = 0; i < matrix.length; i += 1) {
      for (let j = 0; j < (matrix[i] || []).length; j += 1) {
        cells.push([j, i, Number(matrix[i][j])]);
      }
    }
    return {
      ...c,
      grid: { top: 8, right: 8, bottom: 64, left: 8, containLabel: true },
      tooltip: {
        ...c.tooltipBase,
        formatter: (p) => {
          const [x, y, v] = p.value;
          return header(`${assets[y]} vs ${assets[x]}`, t) +
            `<div style="color:${t.text};font-variant-numeric:tabular-nums">${v.toFixed(2)}</div>`;
        },
      },
      xAxis: {
        type: "category", data: assets, ...c.categoryAxis,
        splitArea: { show: false },
        axisLabel: { ...c.categoryAxis.axisLabel, interval: 0, rotate: 35 },
      },
      yAxis: {
        type: "category", data: assets, ...c.categoryAxis,
        // Category axes build upward, which runs the self-correlation diagonal
        // bottom-left to top-right -- the opposite of how a correlation matrix
        // is read anywhere else.
        inverse: true,
        splitArea: { show: false },
      },
      visualMap: {
        min: -1, max: 1, calculable: true, orient: "horizontal",
        left: "center", bottom: 0, itemHeight: 90,
        precision: 2,
        textStyle: { color: t.muted, fontSize: 11 },
        inRange: {
          // Cool -> neutral grey -> warm. The midpoint must not be a hue, and
          // must not be the surface either: --c-grid is within a few points of
          // the panel in dark mode, so a zero-correlation cell read as a hole
          // in the chart rather than as "these two do not move together".
          color: [t.series[0], t.muted, t.series[1]],
        },
      },
      series: [{
        type: "heatmap",
        data: cells,
        // 2px surface gap between cells, per the mark spec.
        itemStyle: { borderColor: t.surface, borderWidth: 2 },
        emphasis: { itemStyle: { borderColor: t.text, borderWidth: 2 } },
      }],
      animation: false,
    };
  }, [assets, matrix, t]);

  const cell = 34;
  return (
    <EChart
      option={option}
      height={height || Math.max(220, assets.length * cell + 150)}
      label={label}
      testId={testId}
    />
  );
}

/**
 * Drift: how far each holding sits from its target weight.
 *
 * Diverging bars around a zero line, sorted by absolute drift, because the
 * question is directional -- overweight and underweight need opposite
 * treatments -- and a paired current/target bar chart buries that sign in the
 * comparison. Two hues with the zero line as the neutral midpoint.
 *
 * Advisory only. This proposes no trades and sizes nothing; `rows` is
 * [{ key, current, target }] as fractions.
 */
export function DriftBars({ rows = [], height, label = "Drift from target weight", testId }) {
  const t = useChartTokens();
  const ordered = useMemo(
    () => [...rows]
      .map((r) => ({ ...r, drift: Number(r.current) - Number(r.target) }))
      .sort((a, b) => Math.abs(a.drift) - Math.abs(b.drift)),
    [rows]
  );
  const option = useMemo(() => {
    if (!t || !ordered.length) return null;
    const c = chrome(t);
    const reach = Math.max(...ordered.map((r) => Math.abs(r.drift)), 0.01);
    return {
      ...c,
      grid: { top: 8, right: 56, bottom: 28, left: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        ...c.tooltipBase,
        axisPointer: { type: "shadow" },
        formatter: (p) => {
          const row = ordered[p[0].dataIndex];
          return header(row.key, t) +
            `<div style="color:${t.text}">Now ${pct(row.current)} · Target ${pct(row.target)}</div>` +
            `<div style="color:${t.muted};margin-top:2px">` +
            `${row.drift >= 0 ? "Overweight" : "Underweight"} by ${pct(Math.abs(row.drift))}</div>`;
        },
      },
      xAxis: {
        type: "value", min: -reach * 1.15, max: reach * 1.15, ...c.valueAxis,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, 0) },
      },
      yAxis: {
        type: "category", data: ordered.map((r) => r.key),
        ...c.categoryAxis, splitLine: { show: false },
      },
      // Two series rather than one with per-item colours, because ECharts does
      // not accept a FUNCTION for label.position: a single series had to pick
      // one side, which parked every label at the bar origin instead of its
      // end. Each row is non-null in exactly one of these.
      series: [
        {
          type: "bar", name: "Overweight", barWidth: "55%", barGap: "-100%",
          data: ordered.map((r) => (r.drift >= 0 ? r.drift : null)),
          itemStyle: { color: t.series[1], borderRadius: [0, 4, 4, 0] },
          label: {
            show: true, position: "right", distance: 8,
            color: t.muted, fontSize: 11,
            formatter: (p) => (p.value == null ? "" : `+${pct(p.value, 0)}`),
          },
        },
        {
          type: "bar", name: "Underweight", barWidth: "55%", barGap: "-100%",
          data: ordered.map((r) => (r.drift < 0 ? r.drift : null)),
          itemStyle: { color: t.series[0], borderRadius: [4, 0, 0, 4] },
          label: {
            show: true, position: "left", distance: 8,
            color: t.muted, fontSize: 11,
            formatter: (p) => (p.value == null ? "" : `\u2212${pct(Math.abs(p.value), 0)}`),
          },
          markLine: {
            silent: true, symbol: "none", label: { show: false },
            lineStyle: { color: t.axis, width: 1 },
            data: [{ xAxis: 0 }],
          },
        },
      ],
      animation: false,
    };
  }, [ordered, t]);

  const rowHeight = 30;
  return (
    <EChart
      option={option}
      height={height || Math.max(160, ordered.length * rowHeight + 60)}
      label={label}
      testId={testId}
    />
  );
}

/**
 * Candidate scatter: what a new holding would do to portfolio RISK (x) against
 * what it returned (y), with the current book overlaid for contrast.
 *
 * The x axis is the point. Ranking candidates by return recommends whatever
 * already went up, which on a concentrated book means more of the same thing;
 * `vol_reduction` asks the different question of whether an asset moves with
 * what you already own. Plotting both keeps the tradeoff visible instead of
 * asserting one answer.
 *
 * `candidates` / `held` are rows from /api/analytics/diversifiers/.
 */
export function DiversifierScatter({
  candidates = [], held = [], height = 380,
  label = "Diversification benefit versus return", testId,
}) {
  const t = useChartTokens();
  const option = useMemo(() => {
    if (!t || !candidates.length) return null;
    const c = chrome(t);
    const axisName = { color: t.muted, fontSize: 11 };
    const point = (r) => ({
      value: [r.vol_reduction, r.total_return],
      name: r.key,
      rho: r.correlation,
    });
    return {
      ...c,
      grid: { top: 16, right: 24, bottom: 52, left: 12, containLabel: true },
      tooltip: {
        trigger: "item",
        ...c.tooltipBase,
        formatter: (p) => {
          const [x, y] = p.value;
          return header(p.data.name, t) +
            `<div style="color:${t.text}">Removes ${pct(x, 2)} of volatility</div>` +
            `<div style="color:${t.text}">Return ${pct(y)}</div>` +
            `<div style="color:${t.muted};margin-top:2px">Correlation ${p.data.rho.toFixed(2)}</div>`;
        },
      },
      legend: c.legend(),
      xAxis: {
        type: "value", ...c.valueAxis, scale: true,
        name: "Volatility removed at a 5% position", nameLocation: "middle",
        nameGap: 30, nameTextStyle: axisName,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, 1) },
      },
      yAxis: {
        type: "value", ...c.valueAxis, scale: true,
        // NOT "over the window": `diversifier_candidates` compounds each
        // candidate over the days it SHARES with the portfolio series, so two
        // dots can cover slightly different day sets. The caption underneath
        // prints how many days that is.
        name: "Return over the measured days", nameLocation: "middle",
        nameGap: 52, nameTextStyle: axisName,
        axisLabel: { ...c.valueAxis.axisLabel, formatter: (v) => pct(v, 0) },
      },
      series: [
        {
          type: "scatter", name: "Candidates", symbolSize: 10,
          data: candidates.map(point),
          itemStyle: { color: t.series[0], opacity: 0.75, borderColor: t.surface, borderWidth: 2 },
        },
        {
          type: "scatter", name: "You already hold", symbolSize: 13,
          data: held.map(point),
          itemStyle: { color: t.series[1], borderColor: t.surface, borderWidth: 2 },
          z: 6,
        },
        {
          // Left of this line an asset ADDS volatility to the book.
          type: "scatter", name: "", data: [], silent: true,
          markLine: {
            silent: true, symbol: "none",
            // No label: ECharts renders text on a vertical markLine rotated
            // and cramped, and the caption beside the chart already says which
            // side is which.
            label: { show: false },
            lineStyle: { color: t.axis, type: "dashed", width: 1 },
            data: [{ xAxis: 0 }],
          },
        },
      ],
      animation: false,
    };
  }, [candidates, held, t]);

  return <EChart option={option} height={height} label={label} testId={testId} />;
}

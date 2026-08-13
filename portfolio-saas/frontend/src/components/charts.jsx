import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ReferenceDot,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { date, dateTick, pct, toman, tomanCompact } from "../format.js";

// Themed recharts wrappers. Pages never import recharts and never write a hex —
// the old code scattered #4c9aff/#3fb950/#f85149 across three files, so a theme
// change meant a grep.
//
// Colors come from the validated categorical palette in index.css, referenced as
// CSS custom properties so light mode swaps without a JS re-render.

export const SERIES = ["var(--c-s1)", "var(--c-s2)", "var(--c-s3)", "var(--c-s4)",
  "var(--c-s5)", "var(--c-s6)", "var(--c-s7)", "var(--c-s8)"];

const INK = "var(--c-muted)";
const GRID = "var(--c-grid)";
const AXIS = "var(--c-axis)";
const SURFACE = "var(--c-panel)";

const axis = { tick: { fontSize: 11, fill: INK }, stroke: AXIS, tickLine: false };

const tooltipProps = {
  cursor: { stroke: AXIS, strokeDasharray: "3 3" },
  contentStyle: {
    background: SURFACE,
    border: "1px solid var(--c-border)",
    borderRadius: 8,
    fontSize: 12,
    color: "var(--c-text)",
  },
  labelStyle: { color: INK, marginBottom: 4 },
  itemStyle: { color: "var(--c-text)" },
};

const legendProps = {
  wrapperStyle: { fontSize: 12, color: INK, paddingTop: 8 },
  iconType: "circle",
  iconSize: 8,
};

const Frame = ({ height, label, children }) => (
  <div className="w-full" style={{ height }} role="img" aria-label={label}>
    <ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer>
  </div>
);

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

/**
 * Net-worth over time. One series, so no legend — the panel title names it.
 * `data` is [{ x: ISO date, y: number }].
 */
export function AreaTrend({ data, height = 260, label = "Portfolio value over time", longTicks }) {
  return (
    <Frame height={height} label={label}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--c-s1)" stopOpacity={0.35} />
            <stop offset="100%" stopColor="var(--c-s1)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="x" {...axis} minTickGap={40} tickFormatter={(v) => dateTick(v, longTicks)} />
        <YAxis
          {...axis}
          width={56}
          tickFormatter={tomanCompact}
          domain={([dataMin, dataMax]) => moneyTrendDomain(dataMin, dataMax)}
          allowDataOverflow={false}
        />
        <Tooltip
          {...tooltipProps}
          labelFormatter={date}
          formatter={(v) => [toman(v), "Value"]}
        />
        <Area
          type="monotone"
          dataKey="y"
          stroke="var(--c-s1)"
          strokeWidth={2}
          fill="url(#trendFill)"
          dot={false}
          activeDot={{ r: 4, stroke: SURFACE, strokeWidth: 2 }}
          isAnimationActive={false}
        />
      </AreaChart>
    </Frame>
  );
}

/**
 * Two-series comparison, e.g. current vs target weight.
 * `data` is [{ name, a, b }]; `labels` is [aLabel, bLabel]. Values are fractions.
 */
export function GroupedBar({ data, labels, height = 300, label = "Comparison" }) {
  return (
    <Frame height={height} label={label}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 8 }} barGap={2}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="name" {...axis} interval={0} angle={-25} textAnchor="end" height={70} />
        <YAxis {...axis} width={48} tickFormatter={(v) => pct(v, 0)} />
        <Tooltip {...tooltipProps} formatter={(v) => pct(v)} />
        <Legend {...legendProps} />
        <Bar dataKey="a" name={labels[0]} fill={SERIES[0]} radius={[4, 4, 0, 0]} isAnimationActive={false} />
        <Bar dataKey="b" name={labels[1]} fill={SERIES[1]} radius={[4, 4, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </Frame>
  );
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
  const total = data.reduce((s, d) => s + Number(d.value || 0), 0) || 1;
  return (
    <div className="flex flex-wrap items-center gap-6" data-testid={testId}>
      <Frame height={height} label={label}>
        <PieChart>
          <Pie
            data={data}
            dataKey="value"
            nameKey="name"
            innerRadius="58%"
            outerRadius="86%"
            paddingAngle={2}
            stroke={SURFACE}
            strokeWidth={2}
            isAnimationActive={false}
          >
            {data.map((d, i) => (
              <Cell key={d.name} fill={SERIES[i % SERIES.length]} />
            ))}
          </Pie>
          <Tooltip
            {...tooltipProps}
            cursor={false}
            formatter={(v, n) => [`${valueFormat(v)} · ${pct(v / total)}`, n]}
          />
        </PieChart>
      </Frame>
      <ul className="min-w-48 flex-1 space-y-1.5 text-sm">
        {data.map((d, i) => (
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
  return (
    <Frame height={height} label={label}>
      <ScatterChart margin={{ top: 8, right: 16, left: 8, bottom: 24 }}>
        <CartesianGrid stroke={GRID} />
        <XAxis
          type="number"
          dataKey="x"
          name="Volatility"
          {...axis}
          tickFormatter={(v) => pct(v, 0)}
          label={{ value: "Annualized volatility", position: "insideBottom", offset: -14, fontSize: 11, fill: INK }}
        />
        <YAxis
          type="number"
          dataKey="y"
          name="Return"
          {...axis}
          width={52}
          tickFormatter={(v) => pct(v, 0)}
          label={{ value: "Annualized return", angle: -90, position: "insideLeft", fontSize: 11, fill: INK }}
        />
        <ZAxis range={[36, 36]} />
        <Tooltip {...tooltipProps} formatter={(v) => pct(v)} />
        {cloud.length > 0 && (
          <Scatter data={cloud} fill={INK} fillOpacity={0.22} shape="circle" isAnimationActive={false} />
        )}
        <Scatter data={frontier} fill={SERIES[0]} isAnimationActive={false} />
        {points.map((p) => (
          <ReferenceDot
            key={p.name}
            x={p.x}
            y={p.y}
            r={7}
            fill={p.color}
            stroke={SURFACE}
            strokeWidth={2}
            isFront
          />
        ))}
      </ScatterChart>
    </Frame>
  );
}

export function CountTrend({ data, height = 180, label = "Count over time", color = "var(--c-s1)" }) {
  return (
    <Frame height={height} label={label}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="x" {...axis} minTickGap={32} tickFormatter={(v) => dateTick(v)} />
        <YAxis {...axis} width={48} tickFormatter={numFmt} />
        <Tooltip {...tooltipProps} labelFormatter={date} formatter={(v) => [numFmt(v), ""]} />
        <Area
          type="monotone"
          dataKey="y"
          stroke={color}
          strokeWidth={2}
          fill={color}
          fillOpacity={0.15}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </Frame>
  );
}

function numFmt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}

export const STATUS_COLOR = {
  good: "var(--c-good)",
  warn: "var(--c-warn)",
  serious: "var(--c-serious)",
  critical: "var(--c-critical)",
};

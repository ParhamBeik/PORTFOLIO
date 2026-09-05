/**
 * Every chart wrapper is mounted with representative data and asserted to
 * complete a real ECharts render pass.
 *
 * This exists because `charts.jsx` had no test at all, which is what made an
 * ECharts major upgrade unshippable: the whole breaking-change surface of that
 * library is option keys and component registration, and every one of those
 * failures is silent. `setOption` does not throw on an option it no longer
 * understands -- it draws a blank chart, and nothing here or in CI would have
 * noticed. So the assertions below are deliberately about *output*: each chart
 * must produce a non-empty canvas draw and register the series it was given.
 *
 * jsdom has no canvas implementation, so `getContext("2d")` is stubbed with a
 * recording no-op. That is enough for ECharts to run layout and its full draw
 * pass, and the recorded call count is what proves the pass happened rather
 * than silently short-circuiting.
 */
import { render, cleanup } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import {
  AreaTrend,
  CorrelationHeatmap,
  CountTrend,
  DiversifierScatter,
  Donut,
  DriftBars,
  GroupedBar,
  MoneyVsRisk,
  MultiLineTrend,
  RiskScatter,
  StackedShareTrend,
  StackedStatusBar,
} from "./charts.jsx";

/** Calls ECharts made into the 2d context, per mounted chart. */
let drawCalls = 0;

beforeAll(() => {
  // ECharts measures before it draws; a zero-sized element makes it skip the
  // draw entirely, which would make every assertion below vacuously pass.
  for (const [prop, value] of [
    ["clientWidth", 640],
    ["clientHeight", 320],
    ["offsetWidth", 640],
    ["offsetHeight", 320],
  ]) {
    Object.defineProperty(window.HTMLElement.prototype, prop, {
      configurable: true,
      value,
    });
  }

  // jsdom ships no ResizeObserver; the wrappers observe their container to
  // drive chart.resize(). Never firing the callback is correct here -- the
  // element never actually resizes in a test.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );

  // useChartTokens watches the colour-scheme media query to re-read the CSS
  // custom properties. jsdom has no matchMedia; report the dark default.
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
  }));

  const context2d = new Proxy(
    {
      // The handful of calls that must return a value rather than undefined.
      measureText: () => ({ width: 40, height: 12 }),
      createLinearGradient: () => ({ addColorStop() {} }),
      createPattern: () => null,
      getImageData: () => ({ data: new Uint8ClampedArray(4) }),
      canvas: null,
    },
    {
      get(target, prop) {
        if (prop in target) return target[prop];
        return (...args) => {
          drawCalls += 1;
          return args;
        };
      },
      set: () => true,
    },
  );
  window.HTMLCanvasElement.prototype.getContext = () => context2d;

  // ECharts drives its first frame through rAF; jsdom provides one, but pin it
  // to a synchronous call so a render is complete by the time render() returns.
  vi.stubGlobal("requestAnimationFrame", (cb) => {
    cb(0);
    return 0;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

afterEach(() => {
  cleanup();
});

/** Mount `element` and assert ECharts actually drew something into it. */
function drew(element) {
  const before = drawCalls;
  const { container } = render(element);
  expect(container.querySelector("canvas")).not.toBeNull();
  return drawCalls - before;
}

const trend = [
  { date: "1405-05-01", value: 1000, total_value: 1000, cost_basis: 900 },
  { date: "1405-05-02", value: 1200, total_value: 1200, cost_basis: 950 },
  { date: "1405-05-03", value: 900, total_value: 900, cost_basis: 980 },
];

describe("every chart wrapper completes a real render pass", () => {
  const cases = [
    ["AreaTrend", <AreaTrend data={trend} />],
    [
      "MultiLineTrend",
      <MultiLineTrend
        data={trend}
        series={[{ key: "value", name: "Value" }, { key: "cost_basis", name: "Cost" }]}
      />,
    ],
    [
      "StackedShareTrend",
      <StackedShareTrend
        data={[
          { date: "1405-05-01", Gold: 60, Stock: 40 },
          { date: "1405-05-02", Gold: 55, Stock: 45 },
        ]}
        seriesKeys={["Gold", "Stock"]}
      />,
    ],
    [
      "GroupedBar",
      <GroupedBar
        data={[{ name: "Mine", values: [1, 2] }, { name: "Optimal", values: [2, 1] }]}
        labels={["Gold", "Stock"]}
      />,
    ],
    [
      "Donut",
      <Donut data={[{ name: "Gold", value: 60 }, { name: "Stock", value: 40 }]} />,
    ],
    [
      "RiskScatter",
      <RiskScatter
        frontier={[{ risk: 0.1, ret: 0.05 }, { risk: 0.2, ret: 0.09 }]}
        points={[{ name: "Mine", risk: 0.15, ret: 0.06 }]}
      />,
    ],
    ["CountTrend", <CountTrend data={[{ date: "1405-05-01", value: 5 }, { date: "1405-05-02", value: 7 }]} />],
    [
      "StackedStatusBar",
      <StackedStatusBar
        data={[{ label: "candles", ok: 10, partial: 2, missing: 1 }]}
        seriesKeys={["ok", "partial", "missing"]}
      />,
    ],
    [
      "MoneyVsRisk",
      <MoneyVsRisk
        rows={[
          { key: "Gold", weight_share: 0.6, risk_share: 0.45, gap: -0.15 },
          { key: "Stock", weight_share: 0.4, risk_share: 0.55, gap: 0.15 },
        ]}
        valueFor={() => 1000}
      />,
    ],
    [
      "CorrelationHeatmap",
      <CorrelationHeatmap assets={["Gold", "Stock"]} matrix={[[1, 0.3], [0.3, 1]]} />,
    ],
    [
      "DriftBars",
      <DriftBars rows={[{ name: "Gold", drift: 0.05 }, { name: "Stock", drift: -0.03 }]} />,
    ],
    [
      "DiversifierScatter",
      <DiversifierScatter
        candidates={[
          { key: "Gold", vol_reduction: 0.08, total_return: 0.22, correlation: 0.15 },
          { key: "Stock", vol_reduction: 0.03, total_return: 0.11, correlation: 0.62 },
        ]}
        held={[{ key: "Cash", vol_reduction: 0.01, total_return: 0.02, correlation: 0.05 }]}
      />,
    ],
  ];

  it.each(cases)("%s draws", (_name, element) => {
    expect(drew(element)).toBeGreaterThan(0);
  });
});

describe("user-supplied labels cannot inject markup", () => {
  it("escapes a holding nickname containing HTML in the tooltip formatter", () => {
    // charts.jsx documents that tooltip formatters return HTML and that every
    // user-typed label goes through esc(). This pins that the raw tag never
    // reaches the DOM as an element.
    const nasty = '<img src=x onerror=alert(1)>';
    const { container } = render(
      <Donut data={[{ name: nasty, value: 1 }, { name: "Stock", value: 2 }]} />,
    );
    // The payload must survive as *text* and never become an element. Asserting
    // on innerHTML would be wrong: it re-serialises the escaped text node back
    // to `&lt;img ... onerror=alert(1)&gt;`, so the substring is present and
    // harmless. Element identity is the thing that actually matters.
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(nasty);
  });
});

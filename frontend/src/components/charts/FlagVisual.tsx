import type { TooltipComponentFormatterCallbackParams } from "echarts";
import { useMemo } from "react";
import type { components } from "../../api/types";
import { percent, signed } from "../format";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { AXIS_COLOUR, MOVER_COLOURS, SERIES_COLOURS } from "./theme";
import TierStrip from "./TierStrip";
import { escapeHtml } from "./tooltip";

type Schemas = components["schemas"];
type RaisedFlag = Schemas["RaisedFlag"];
type Detail = RaisedFlag["detail"];

/** A bar of a flag's drawing: its axis label, its length, its colour and its value as hover says it. */
type Bar = { label: string; value: number; colour: string; text: string };

const num = (value: unknown) => (typeof value === "number" ? value : null);

// The deltas are fractions; their flags read in percentage points.
const points = (value: number) => value * 100;

/**
 * A small drawing of the numbers that raised a flag, from its `detail` (and a yo-yo's `tiers`), the
 * same numbers its text line gives (v2 spec §6.2):
 *
 * - minutes cliff: his 5-game and 20-game minutes share as bars on one scale, the 20-game's 60%
 *   marked, the line the 5-game fell below;
 * - earning trust: his three 5-game shares, the oldest first, as a rising step line;
 * - bleeding: his on-court and off-court net as bars from zero;
 * - usage up, efficiency down: his usage and true shooting deltas, in points, as bars from zero;
 * - yo-yo: his `TierStrip`;
 * - contract pressure: nothing, nor for a flag whose numbers are missing.
 */
function FlagVisual({ raised }: { raised: RaisedFlag }) {
  const option = useMemo(() => flagOption(raised.flag, raised.detail), [raised.flag, raised.detail]);
  if (raised.flag === "yo_yo") return raised.tiers ? <TierStrip tiers={raised.tiers} /> : null;
  return option ? <EChart option={option} className="chart-flag" /> : null;
}

function flagOption(flag: Schemas["Flag"], detail: Detail): ChartOption | null {
  switch (flag) {
    case "minutes_cliff": {
      const [share5, share20] = [num(detail.share_5), num(detail.share_20)];
      if (share5 === null || share20 === null) return null;
      return barsOption(
        [
          { label: "5g", value: points(share5), colour: MOVER_COLOURS.falling, text: percent(share5) },
          { label: "20g", value: points(share20), colour: AXIS_COLOUR, text: percent(share20) },
        ],
        { min: 0, max: points(Math.max(share5, share20)) },
        // Where the cliff is: below 60% of the 20-game share (`FlagRule.cliff_ratio`).
        points(0.6 * share20),
      );
    }
    case "bleeding": {
      const [on, off] = [num(detail.on_court_net), num(detail.off_court_net)];
      if (on === null || off === null) return null;
      return barsOption(signedBars([["On", on, signed(on)], ["Off", off, signed(off)]]), around([on, off]), 0);
    }
    case "usage_up_efficiency_down": {
      const [usage, ts] = [num(detail.usg_pct_delta_season), num(detail.ts_pct_delta_season)];
      if (usage === null || ts === null) return null;
      const [usagePts, tsPts] = [points(usage), points(ts)];
      return barsOption(
        signedBars([
          ["USG", usagePts, `${signed(usagePts)} pts`],
          ["TS", tsPts, `${signed(tsPts)} pts`],
        ]),
        around([usagePts, tsPts]),
        0,
      );
    }
    case "earning_trust": {
      const shares = Array.isArray(detail.shares) ? detail.shares.map(num) : [];
      if (shares.length !== 3 || shares.some((share) => share === null)) return null;
      return trustOption(shares as number[]);
    }
    case "yo_yo":
    case "contract_pressure":
      return null;
  }
}

/** Bars from zero, each in the rising colour if positive and the falling colour if not. */
function signedBars(rows: [label: string, value: number, text: string][]): Bar[] {
  return rows.map(([label, value, text]) => ({
    label,
    value,
    text,
    colour: value > 0 ? MOVER_COLOURS.rising : MOVER_COLOURS.falling,
  }));
}

/** A range centred on zero, reaching the largest of `values` either way. */
function around(values: number[]) {
  const half = Math.max(...values.map(Math.abs)) || 1;
  return { min: -half, max: half };
}

function itemTooltip(
  text: (dataIndex: number) => string | undefined,
): (params: TooltipComponentFormatterCallbackParams) => string {
  return (params) => {
    const item = Array.isArray(params) ? params[0] : params;
    return item === undefined ? "" : escapeHtml(text(item.dataIndex) ?? "");
  };
}

/** Horizontal bars, the first on top, over `range`, with a solid line at `mark` if given. */
function barsOption(bars: Bar[], range: { min: number; max: number }, mark?: number): ChartOption {
  return {
    animation: false,
    grid: { top: 2, right: 4, bottom: 2, left: 28 },
    // On the page's body, as the drawing is too small to hold it.
    tooltip: {
      trigger: "item",
      appendTo: "body",
      formatter: itemTooltip((i) => bars[i] && `${bars[i].label}: ${bars[i].text}`),
    },
    xAxis: { type: "value", show: false, min: range.min, max: range.max },
    yAxis: {
      type: "category",
      inverse: true,
      data: bars.map((bar) => bar.label),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { fontSize: 9, color: AXIS_COLOUR, margin: 4 },
    },
    series: [
      {
        type: "bar",
        barCategoryGap: "25%",
        data: bars.map((bar) => ({ value: bar.value, itemStyle: { color: bar.colour } })),
        ...(mark !== undefined && {
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: false },
            lineStyle: { type: mark === 0 ? "solid" : "dashed", width: 1, color: AXIS_COLOUR },
            data: [{ xAxis: mark }],
          },
        }),
      },
    ],
  };
}

const TRUST_WINDOWS = ["Games 11–15 back", "Games 6–10 back", "Last 5 games"];

/** His three 5-game minutes shares, the oldest first, as a step line from zero. */
function trustOption(shares: number[]): ChartOption {
  return {
    animation: false,
    grid: { top: 4, right: 4, bottom: 4, left: 4 },
    tooltip: {
      trigger: "item",
      appendTo: "body",
      formatter: itemTooltip((i) => `${TRUST_WINDOWS[i]}: ${percent(shares[i])}`),
    },
    xAxis: { type: "category", show: false, boundaryGap: false, data: TRUST_WINDOWS },
    yAxis: { type: "value", show: false, min: 0 },
    series: [
      {
        type: "line",
        step: "middle",
        data: shares.map(points),
        symbol: "circle",
        symbolSize: 4,
        lineStyle: { width: 1.5, color: SERIES_COLOURS[0] },
        itemStyle: { color: SERIES_COLOURS[0] },
      },
    ],
  };
}

export default FlagVisual;

import { useMemo } from "react";
import type { components } from "../../api/types";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { AXIS_COLOUR, SERIES_COLOURS } from "./theme";

type Point = components["schemas"]["Point"];

/** An axis-less line for a table cell, a game date per point, with an optional dashed baseline. */
function SparklineCell({ series, baseline }: { series: Point[]; baseline?: number }) {
  const option = useMemo(() => sparklineOption(series, baseline), [series, baseline]);
  return <EChart option={option} className="chart-sparkline" />;
}

function sparklineOption(series: Point[], baseline: number | undefined): ChartOption {
  return {
    animation: false,
    grid: { top: 2, right: 2, bottom: 2, left: 2 },
    xAxis: {
      type: "category",
      show: false,
      boundaryGap: false,
      data: series.map((point) => point.game_date),
    },
    // Widened to take the baseline in, so a series wholly above or below it still shows where it is.
    yAxis: {
      type: "value",
      show: false,
      scale: true,
      min: ({ min }: { min: number }) => (baseline === undefined ? min : Math.min(min, baseline)),
      max: ({ max }: { max: number }) => (baseline === undefined ? max : Math.max(max, baseline)),
    },
    series: [
      {
        type: "line",
        data: series.map((point) => point.value),
        connectNulls: false,
        showSymbol: false,
        silent: true,
        lineStyle: { width: 1.5, color: SERIES_COLOURS[0] },
        ...(baseline !== undefined && {
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: false },
            lineStyle: { type: "dashed", width: 1, color: AXIS_COLOUR },
            data: [{ yAxis: baseline }],
          },
        }),
      },
    ],
  };
}

export default SparklineCell;

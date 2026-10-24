import { useMemo } from "react";
import type { components } from "../../api/types";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { AXIS_COLOUR, SERIES_COLOURS } from "./theme";

type Point = components["schemas"]["Point"];

/** A y range several cells share, so their lines read against one another. */
export type SparklineRange = { min: number; max: number };

/**
 * An axis-less line for a table cell, a game date per point, with an optional dashed baseline; its
 * y range is its own unless `range` is given.
 */
function SparklineCell({
  series,
  baseline,
  range,
}: {
  series: Point[];
  baseline?: number;
  range?: SparklineRange;
}) {
  const option = useMemo(() => sparklineOption(series, baseline, range), [series, baseline, range]);
  return <EChart option={option} className="chart-sparkline" />;
}

function sparklineOption(
  series: Point[],
  baseline: number | undefined,
  range: SparklineRange | undefined,
): ChartOption {
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
      min: ({ min }: { min: number }) => {
        const low = range?.min ?? min;
        return baseline === undefined ? low : Math.min(low, baseline);
      },
      max: ({ max }: { max: number }) => {
        const high = range?.max ?? max;
        return baseline === undefined ? high : Math.max(high, baseline);
      },
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

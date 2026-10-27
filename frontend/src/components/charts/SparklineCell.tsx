import { useMemo } from "react";
import type { TooltipComponentFormatterCallbackParams } from "echarts";
import type { components } from "../../api/types";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { AXIS_COLOUR, SERIES_COLOURS } from "./theme";
import { escapeHtml } from "./tooltip";

type Point = components["schemas"]["Point"];

/** A y range several cells share, so their lines read against one another. */
export type SparklineRange = { min: number; max: number };

// Room for the end label at its font size: on the right for its width, and above and below for half
// its height, as it is centred on a point that can sit at the top or bottom of the range.
const END_LABEL_ROOM = 40;
const END_LABEL_HALF_HEIGHT = 7;

/**
 * An axis-less line for a table cell, a game date per point, with an optional dashed baseline; its
 * y range is its own unless `range` is given. `endLabel` writes the latest value at the line's end,
 * and hovering shows a point's date and value, both through `format`.
 */
function SparklineCell({
  series,
  baseline,
  range,
  endLabel = false,
  format = String,
}: {
  series: Point[];
  baseline?: number;
  range?: SparklineRange;
  endLabel?: boolean;
  format?: (value: number) => string;
}) {
  const option = useMemo(
    () => sparklineOption(series, baseline, range, endLabel, format),
    [series, baseline, range, endLabel, format],
  );
  return <EChart option={option} className="chart-sparkline" />;
}

function sparklineOption(
  series: Point[],
  baseline: number | undefined,
  range: SparklineRange | undefined,
  endLabel: boolean,
  format: (value: number) => string,
): ChartOption {
  // ECharts puts the end label on the last point with a value, so it reads that point's.
  const latest = series.flatMap((point) => point.value ?? []).at(-1);
  const labelled = endLabel && latest !== undefined;
  // A value with none either side has no line to draw, so it is drawn as a dot.
  const isolated = series.map((point, i) =>
    point.value !== null && series[i - 1]?.value == null && series[i + 1]?.value == null
      ? point.value
      : null,
  );

  const formatTooltip = (params: TooltipComponentFormatterCallbackParams) => {
    const item = Array.isArray(params) ? params[0] : params;
    const value = typeof item?.value === "number" ? format(item.value) : "—";
    return `${escapeHtml(item?.name ?? "")}<br>${escapeHtml(value)}`;
  };

  return {
    animation: false,
    grid: {
      top: labelled ? END_LABEL_HALF_HEIGHT : 2,
      right: labelled ? END_LABEL_ROOM : 2,
      bottom: labelled ? END_LABEL_HALF_HEIGHT : 2,
      left: 2,
    },
    // On the page's body, as a cell is too small to hold it.
    tooltip: { trigger: "axis", formatter: formatTooltip, appendTo: "body" },
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
        lineStyle: { width: 1.5, color: SERIES_COLOURS[0] },
        ...(labelled && {
          endLabel: {
            show: true,
            formatter: () => format(latest),
            fontSize: 10,
            distance: 4,
            color: SERIES_COLOURS[0],
          },
        }),
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
      ...(isolated.some((value) => value !== null)
        ? [
            {
              type: "line" as const,
              data: isolated,
              symbol: "circle",
              symbolSize: 4,
              lineStyle: { width: 0 },
              itemStyle: { color: SERIES_COLOURS[0] },
              silent: true,
            },
          ]
        : []),
    ],
  };
}

export default SparklineCell;

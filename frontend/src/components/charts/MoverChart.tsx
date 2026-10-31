import type { ECElementEvent, TooltipComponentFormatterCallbackParams } from "echarts";
import { useCallback, useMemo } from "react";
import { useLocation, useNavigate } from "react-router";
import type { components } from "../../api/types";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { AXIS_COLOUR, LABEL_COLOUR, MOVER_COLOURS } from "./theme";
import { dot, escapeHtml } from "./tooltip";

type Mover = components["schemas"]["Mover"];

const ROW_HEIGHT = 20;
// Room under the bars for the axis's tick labels and its name.
const AXIS_ROOM = 48;

/**
 * Risers and fallers on one diverging scale: a bar per mover from a zero line, risers' to the right
 * in the rising colour above fallers' to the left in the falling colour, the largest rise at the top
 * and the largest fall at the bottom. A bar's length is the mover's `delta` times `scale` (100 turns
 * a share into percentage points), on an x axis named `unitLabel` and centred on zero, so a rise and
 * a fall of one size are bars of one length. Each bar carries "Name TRI" on the far side of the zero
 * line, hovering shows the prior and latest values through `format`, and clicking a bar goes to the
 * player's page, keeping the search.
 */
function MoverChart({
  risers,
  fallers,
  format,
  unitLabel,
  scale = 1,
}: {
  risers: Mover[];
  fallers: Mover[];
  format: (value: number | null) => string;
  unitLabel: string;
  scale?: number;
}) {
  const navigate = useNavigate();
  const { search } = useLocation();
  const rows = useMemo(
    () => [...risers, ...fallers].sort((a, b) => b.delta - a.delta),
    [risers, fallers],
  );
  const option = useMemo(
    () => moverOption(rows, format, unitLabel, scale),
    [rows, format, unitLabel, scale],
  );
  const onClick = useCallback(
    (event: ECElementEvent) => {
      const mover = rows[event.dataIndex];
      if (event.componentType === "series" && mover) {
        void navigate({ pathname: `/players/${mover.player.player_id}`, search });
      }
    },
    [rows, navigate, search],
  );
  return (
    <EChart
      option={option}
      onClick={onClick}
      className="chart"
      style={{ height: rows.length * ROW_HEIGHT + AXIS_ROOM }}
    />
  );
}

function moverLabel(mover: Mover) {
  const name = mover.player.name ?? `Player ${mover.player.player_id}`;
  return mover.tricode ? `${name} ${mover.tricode}` : name;
}

// The step multiples a tick may fall on, in each power of ten.
const NICE_STEPS = [1, 2, 2.5, 5, 10];

/**
 * A tick step giving about three ticks either side of zero, and the axis's half-width, `extent`
 * rounded up to a whole number of steps: 7 → step 2.5 and half-width 7.5; 0.83 → 0.5 and 1.
 */
function niceAxis(extent: number) {
  // Rounded first, so a float's tail (7.000000000000001) does not add a step.
  const rounded = Number(extent.toPrecision(6));
  if (rounded <= 0) return { step: 1, half: 1 };
  const raw = rounded / 3;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = NICE_STEPS.map((nice) => nice * magnitude).find((nice) => nice >= raw)!;
  return { step, half: Math.ceil(rounded / step) * step };
}

/** A tick on the axis: its sign, no padding, a true minus. */
function tick(value: number) {
  const rounded = Number(value.toPrecision(6));
  if (rounded === 0) return "0";
  return rounded > 0 ? `+${rounded}` : `−${-rounded}`;
}

function moverOption(
  rows: Mover[],
  format: (value: number | null) => string,
  unitLabel: string,
  scale: number,
): ChartOption {
  const { step, half } = niceAxis(
    Math.max(0, ...rows.map((mover) => Math.abs(mover.delta * scale))),
  );

  const formatTooltip = (params: TooltipComponentFormatterCallbackParams) => {
    const item = Array.isArray(params) ? params[0] : params;
    const mover = item === undefined ? undefined : rows[item.dataIndex];
    if (mover === undefined) return "";
    const prior = mover.value === null ? null : mover.value - mover.delta;
    const colour = mover.delta > 0 ? MOVER_COLOURS.rising : MOVER_COLOURS.falling;
    return (
      `${dot(colour)}${escapeHtml(moverLabel(mover))}<br>` +
      `${escapeHtml(format(prior))} → ${escapeHtml(format(mover.value))}`
    );
  };

  return {
    animation: false,
    grid: { top: 4, right: 16, bottom: AXIS_ROOM, left: 16 },
    tooltip: { trigger: "item", formatter: formatTooltip },
    xAxis: {
      type: "value",
      name: unitLabel,
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: LABEL_COLOUR },
      min: -half,
      max: half,
      interval: step,
      axisLabel: { formatter: tick, color: AXIS_COLOUR },
      splitLine: { lineStyle: { color: AXIS_COLOUR, opacity: 0.25 } },
    },
    // The names ride on the bars, so the category axis is only their rows.
    yAxis: { type: "category", inverse: true, show: false, data: rows.map(moverLabel) },
    series: [
      {
        type: "bar",
        barCategoryGap: 2,
        cursor: "pointer",
        label: { show: true, formatter: "{b}", color: LABEL_COLOUR, fontSize: 11 },
        data: rows.map((mover) => {
          const rising = mover.delta > 0;
          return {
            value: mover.delta * scale,
            itemStyle: {
              color: rising ? MOVER_COLOURS.rising : MOVER_COLOURS.falling,
              // Rounded at the bar's end, square where it meets the zero line.
              borderRadius: rising ? [0, 4, 4, 0] : [4, 0, 0, 4],
            },
            // On the far side of the zero line, clear of the bar.
            label: { position: rising ? ("left" as const) : ("right" as const) },
          };
        }),
        markLine: {
          silent: true,
          symbol: "none",
          label: { show: false },
          lineStyle: { type: "solid", width: 1, color: AXIS_COLOUR },
          data: [{ xAxis: 0 }],
        },
      },
    ],
  };
}

export default MoverChart;

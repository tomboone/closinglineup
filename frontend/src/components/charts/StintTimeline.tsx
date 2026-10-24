import type { CustomSeriesRenderItem } from "echarts";
import { useMemo } from "react";
import type { components } from "../../api/types";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { AXIS_COLOUR, GARBAGE_TIME_FILL, TEAM_COLOURS } from "./theme";
import { escapeHtml } from "./tooltip";

type Schemas = components["schemas"];

/** A player's timeline row: his name, his team's tricode and his stints, seconds into the period. */
export type StintRow = {
  label: string;
  team: string;
  stints: Pick<Schemas["PlayerStint"], "period" | "start_sec" | "end_sec" | "is_garbage_time">[];
};

const REGULATION_PERIODS = 4;
const PERIOD_SECONDS = 720;
const OVERTIME_SECONDS = 300;
const ROW_HEIGHT = 22;

function periodLength(period: number) {
  return period <= REGULATION_PERIODS ? PERIOD_SECONDS : OVERTIME_SECONDS;
}

/** Seconds since tip-off at `sec` seconds into `period`. */
function gameSeconds(period: number, sec: number) {
  let elapsed = sec;
  for (let p = 1; p < period; p++) elapsed += periodLength(p);
  return elapsed;
}

function periodName(period: number) {
  return period <= REGULATION_PERIODS ? `Q${period}` : `OT${period - REGULATION_PERIODS}`;
}

/** The game clock, which counts down, at `sec` seconds into `period`, to the nearest second. */
function clock(period: number, sec: number) {
  const left = Math.round(periodLength(period) - sec);
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

function stintTooltip(row: StintRow, stint: StintRow["stints"][number]) {
  const span = `${periodName(stint.period)} ${clock(stint.period, stint.start_sec)}–${clock(stint.period, stint.end_sec)}`;
  const garbage = stint.is_garbage_time ? "<br>Garbage time" : "";
  return `${escapeHtml(row.label)} (${escapeHtml(row.team)})<br>${span}${garbage}`;
}

// A bar's value is [start, end, row, in garbage time (1) or not (0)], both ends in game seconds.
const renderStint: CustomSeriesRenderItem = (_params, api) => {
  const row = api.value(2);
  const [x0, y] = api.coord([api.value(0), row]);
  const [x1] = api.coord([api.value(1), row]);
  const height = (api.size!([0, 1]) as number[])[1] * 0.6;
  return {
    type: "rect",
    shape: { x: x0, y: y - height / 2, width: Math.max(x1 - x0, 1), height },
    style: { fill: api.visual("color") as string, opacity: api.value(3) ? 0.4 : 1 },
  };
};

/**
 * A horizontal gantt of who was on the floor: a row per player, grouped by team in the order teams
 * first appear in `rows`, a line at each period boundary, and garbage time shaded from
 * `garbageTimeFrom` to the final buzzer. Sizes its own height to the rows.
 */
function StintTimeline({
  rows,
  periods,
  garbageTimeFrom,
}: {
  rows: StintRow[];
  periods: number;
  garbageTimeFrom: Schemas["GarbageTimeMarker"] | null;
}) {
  const option = useMemo(
    () => stintTimelineOption(rows, periods, garbageTimeFrom),
    [rows, periods, garbageTimeFrom],
  );
  return <EChart option={option} className="chart" style={{ height: rows.length * ROW_HEIGHT + 64 }} />;
}

function stintTimelineOption(
  rows: StintRow[],
  periods: number,
  garbageTimeFrom: Schemas["GarbageTimeMarker"] | null,
): ChartOption {
  const teams = [...new Set(rows.map((row) => row.team))];
  const grouped = teams.flatMap((team) => rows.filter((row) => row.team === team));
  const periodStarts = Array.from({ length: periods }, (_, i) => gameSeconds(i + 1, 0));
  const end = gameSeconds(periods + 1, 0);

  const teamSeries = teams.map((team, t) => ({
    type: "custom" as const,
    name: team,
    renderItem: renderStint,
    encode: { x: [0, 1], y: 2 },
    itemStyle: { color: TEAM_COLOURS[t % TEAM_COLOURS.length] },
    data: grouped.flatMap((row, r) =>
      row.team === team
        ? row.stints.map((stint) => ({
            value: [
              gameSeconds(stint.period, stint.start_sec),
              gameSeconds(stint.period, stint.end_sec),
              r,
              stint.is_garbage_time ? 1 : 0,
            ],
            tooltip: { formatter: stintTooltip(row, stint) },
          }))
        : [],
    ),
  }));

  // Period boundaries and garbage time belong to the game, not to a team, so they ride on a series
  // of their own that the legend leaves out.
  const gameSeries = {
    type: "line" as const,
    name: "Game",
    data: [],
    markLine: {
      silent: true,
      symbol: "none",
      animation: false,
      label: { show: false },
      lineStyle: { color: AXIS_COLOUR, type: "solid" as const, width: 1 },
      data: periodStarts.slice(1).map((start) => ({ xAxis: start })),
    },
    ...(garbageTimeFrom && {
      markArea: {
        silent: true,
        itemStyle: { color: GARBAGE_TIME_FILL },
        label: { show: true, position: "insideTop" as const, color: AXIS_COLOUR },
        data: [
          [
            { name: "Garbage time", xAxis: gameSeconds(garbageTimeFrom.period, garbageTimeFrom.start_sec) },
            { xAxis: end },
          ] as [{ name: string; xAxis: number }, { xAxis: number }],
        ],
      },
    }),
  };

  return {
    animation: false,
    grid: { top: 32, right: 24, bottom: 32, left: 140 },
    legend: { top: 0, data: teams },
    tooltip: { trigger: "item" },
    xAxis: {
      type: "value",
      min: 0,
      max: end,
      splitLine: { show: false },
      axisTick: { customValues: periodStarts },
      axisLabel: {
        customValues: periodStarts,
        formatter: (value: number) => periodName(periodStarts.indexOf(value) + 1),
      },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: grouped.map((row) => row.label),
      axisLabel: {
        color: (_value?: string | number, index?: number) =>
          TEAM_COLOURS[teams.indexOf(grouped[index ?? 0].team) % TEAM_COLOURS.length],
      },
    },
    series: [...teamSeries, gameSeries],
  };
}

export default StintTimeline;

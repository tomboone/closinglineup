import { render } from "@testing-library/react";
import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemParams } from "echarts";
import { describe, expect, it, vi } from "vitest";
import { lastOption } from "../../test/echarts";
import StintTimeline from "./StintTimeline";
import type { StintRow } from "./StintTimeline";
import { GARBAGE_TIME_FILL, TEAM_COLOURS } from "./theme";

vi.mock("./echarts", () => import("../../test/echarts"));

// Interleaved by team, as a caller might pass them; the chart groups them.
const ROWS: StintRow[] = [
  {
    label: "Jalen Brunson",
    team: "NYK",
    stints: [
      { period: 1, start_sec: 0, end_sec: 437.3, is_garbage_time: false },
      { period: 5, start_sec: 0, end_sec: 300, is_garbage_time: false },
    ],
  },
  {
    label: "Jayson Tatum",
    team: "BOS",
    stints: [{ period: 4, start_sec: 500, end_sec: 720, is_garbage_time: true }],
  },
  {
    label: "Josh Hart",
    team: "NYK",
    stints: [{ period: 2, start_sec: 120, end_sec: 720, is_garbage_time: null }],
  },
];

type Series = {
  type: string;
  name: string;
  data?: { value: number[] }[];
  markLine?: { data: { xAxis: number }[] };
  markArea?: { itemStyle: { color: string }; data: [{ xAxis: number }, { xAxis: number }][] };
  renderItem?: (params: CustomSeriesRenderItemParams, api: CustomSeriesRenderItemAPI) => unknown;
};

function renderChart(garbageTimeFrom: { period: number; start_sec: number } | null) {
  render(<StintTimeline rows={ROWS} periods={5} garbageTimeFrom={garbageTimeFrom} />);
  const option = lastOption();
  return { option, series: option.series as Series[] };
}

describe("StintTimeline", () => {
  it("draws a row per player, grouped by team in the order teams first appear", () => {
    const { option } = renderChart(null);

    expect(option.yAxis).toMatchObject({
      type: "category",
      inverse: true,
      data: ["Jalen Brunson", "Josh Hart", "Jayson Tatum"],
    });
  });

  it("draws each team's stints as one series of bars in game seconds, overtime 5 minutes", () => {
    const { option, series } = renderChart(null);

    const teams = series.filter((s) => s.type === "custom");
    expect(teams).toMatchObject([
      {
        name: "NYK",
        itemStyle: { color: TEAM_COLOURS[0] },
        data: [{ value: [0, 437.3, 0, 0] }, { value: [2880, 3180, 0, 0] }, { value: [840, 1440, 1, 0] }],
      },
      { name: "BOS", itemStyle: { color: TEAM_COLOURS[1] }, data: [{ value: [2660, 2880, 2, 1] }] },
    ]);
    expect(option.xAxis).toMatchObject({ type: "value", min: 0, max: 3180 });
    expect(option.legend).toMatchObject({ data: ["NYK", "BOS"] });
  });

  it("labels a stint's tooltip with its period and the game clock, counting down to the second", () => {
    const { series } = renderChart(null);

    const tooltips = series
      .flatMap((s) => (s.data ?? []) as { tooltip?: { formatter: string } }[])
      .map((d) => d.tooltip?.formatter);
    expect(tooltips).toEqual([
      "Jalen Brunson (NYK)<br>Q1 12:00–4:43",
      "Jalen Brunson (NYK)<br>OT1 5:00–0:00",
      "Josh Hart (NYK)<br>Q2 10:00–0:00",
      "Jayson Tatum (BOS)<br>Q4 3:40–0:00<br>Garbage time",
    ]);
  });

  it("marks each period boundary", () => {
    const { series } = renderChart(null);

    const boundaries = series.flatMap((s) => s.markLine?.data ?? []).map((d) => d.xAxis);
    expect(boundaries).toEqual([720, 1440, 2160, 2880]);
  });

  it("shades from garbageTimeFrom to the end of the game", () => {
    const { series } = renderChart({ period: 4, start_sec: 500 });

    const areas = series.flatMap((s) => (s.markArea ? [s.markArea] : []));
    expect(areas).toHaveLength(1);
    expect(areas[0]).toMatchObject({
      itemStyle: { color: GARBAGE_TIME_FILL },
      data: [[{ xAxis: 2660 }, { xAxis: 3180 }]],
    });
  });

  it("shades nothing when the game had no garbage time", () => {
    const { series } = renderChart(null);

    expect(series.some((s) => s.markArea)).toBe(false);
  });

  it("renders a stint as a bar from its start to its end on its row, faded in garbage time", () => {
    const { series } = renderChart(null);
    const renderItem = series.find((s) => s.type === "custom")!.renderItem!;
    const values = [2660, 2880, 2, 1];
    const api = {
      value: (dim: number) => values[dim],
      coord: ([x, y]: number[]) => [x / 10, y * 20],
      size: () => [0, 20],
      visual: () => "#000",
    } as unknown as CustomSeriesRenderItemAPI;

    expect(renderItem({} as CustomSeriesRenderItemParams, api)).toMatchObject({
      type: "rect",
      shape: { x: 266, width: 22, y: 34, height: 12 },
      style: { fill: "#000", opacity: 0.4 },
    });
  });
});

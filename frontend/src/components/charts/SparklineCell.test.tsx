import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { lastOption } from "../../test/echarts";
import SparklineCell from "./SparklineCell";

vi.mock("./echarts", () => import("../../test/echarts"));

const SERIES = [
  { game_date: "2026-03-17", value: 0.12 },
  { game_date: "2026-03-18", value: null },
  { game_date: "2026-03-20", value: 0.15 },
];

describe("SparklineCell", () => {
  it("draws one bare line over the series' dates, leaving a gap for a missing value", () => {
    render(<SparklineCell series={SERIES} />);

    const option = lastOption();
    expect(option.xAxis).toMatchObject({
      show: false,
      data: ["2026-03-17", "2026-03-18", "2026-03-20"],
    });
    expect(option.series).toHaveLength(1);
    expect(option.series).toMatchObject([
      { type: "line", data: [0.12, null, 0.15], connectNulls: false, showSymbol: false },
    ]);
    expect((option.series as object[])[0]).not.toHaveProperty("markLine");
  });

  it("marks the baseline and keeps it inside the y range", () => {
    render(<SparklineCell series={SERIES} baseline={0.2} />);

    const option = lastOption();
    expect(option.series).toMatchObject([{ markLine: { data: [{ yAxis: 0.2 }] } }]);
    const yAxis = option.yAxis as { min: (r: { min: number }) => number; max: (r: { max: number }) => number };
    expect(yAxis.min({ min: 0.12 })).toBe(0.12);
    expect(yAxis.max({ max: 0.15 })).toBe(0.2);
  });

  it("takes a shared y range in place of its own", () => {
    render(<SparklineCell series={SERIES} range={{ min: 0.05, max: 0.3 }} />);

    const yAxis = lastOption().yAxis as {
      min: (r: { min: number }) => number;
      max: (r: { max: number }) => number;
    };
    expect(yAxis.min({ min: 0.12 })).toBe(0.05);
    expect(yAxis.max({ max: 0.15 })).toBe(0.3);
  });
});

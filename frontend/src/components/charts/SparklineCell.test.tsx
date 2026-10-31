import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { lastOption } from "../../test/echarts";
import SparklineCell from "./SparklineCell";

vi.mock("./echarts", () => import("../../test/echarts"));

const SERIES = [
  { game_date: "2026-03-15", value: 0.11 },
  { game_date: "2026-03-17", value: 0.12 },
  { game_date: "2026-03-18", value: null },
  { game_date: "2026-03-20", value: 0.15 },
  { game_date: "2026-03-22", value: 0.14 },
];

describe("SparklineCell", () => {
  it("draws one bare line over the series' dates, leaving a gap for a missing value", () => {
    render(<SparklineCell series={SERIES} />);

    const option = lastOption();
    expect(option.xAxis).toMatchObject({
      show: false,
      data: ["2026-03-15", "2026-03-17", "2026-03-18", "2026-03-20", "2026-03-22"],
    });
    expect(option.series).toHaveLength(1);
    expect(option.series).toMatchObject([
      { type: "line", data: [0.11, 0.12, null, 0.15, 0.14], connectNulls: false, showSymbol: false },
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

  it("labels the line's end with its latest value through the formatter, only when asked", () => {
    const format = (value: number) => `${(value * 100).toFixed(1)}%`;
    render(
      <SparklineCell
        series={[...SERIES, { game_date: "2026-03-24", value: null }]}
        endLabel
        format={format}
      />,
    );

    type Line = { endLabel: { show: boolean; formatter: () => string } };
    const [line] = lastOption().series as Line[];
    expect(line.endLabel.show).toBe(true);
    expect(line.endLabel.formatter()).toBe("14.0%");

    render(<SparklineCell series={SERIES} format={format} />);
    expect((lastOption().series as object[])[0]).not.toHaveProperty("endLabel");
  });

  it("draws a dot for a value with no value either side, as it has no line", () => {
    render(
      <SparklineCell
        series={[
          { game_date: "2026-03-15", value: null },
          { game_date: "2026-03-17", value: 4.6 },
          { game_date: "2026-03-18", value: null },
          { game_date: "2026-03-20", value: 1.2 },
          { game_date: "2026-03-22", value: 1.4 },
        ]}
      />,
    );

    expect(lastOption().series).toMatchObject([
      { data: [null, 4.6, null, 1.2, 1.4], showSymbol: false },
      { type: "line", data: [null, 4.6, null, null, null], symbol: "circle" },
    ]);
  });

  it("leaves room above and below for the end label, which centres on a point at either edge", () => {
    render(<SparklineCell series={SERIES} endLabel />);
    expect(lastOption().grid).toMatchObject({ top: 7, right: 40, bottom: 7 });

    render(<SparklineCell series={SERIES} />);
    expect(lastOption().grid).toMatchObject({ top: 2, right: 2, bottom: 2 });
  });

  it("leaves the end label off a series with no values", () => {
    render(<SparklineCell series={[{ game_date: "2026-03-17", value: null }]} endLabel />);

    expect((lastOption().series as object[])[0]).not.toHaveProperty("endLabel");
  });

  it("shows a hovered point's date and formatted value", () => {
    render(<SparklineCell series={SERIES} format={(value) => `<${value}>`} />);

    const tooltip = lastOption().tooltip as {
      trigger: string;
      formatter: (params: { name: string; value: number | null }[]) => string;
    };
    expect(tooltip.trigger).toBe("axis");
    expect(tooltip.formatter([{ name: "2026-03-20", value: 0.15 }])).toBe(
      "2026-03-20<br>&lt;0.15&gt;",
    );
    expect(tooltip.formatter([{ name: "2026-03-18", value: null }])).toBe("2026-03-18<br>—");
  });
});

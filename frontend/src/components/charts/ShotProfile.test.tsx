import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { lastOption } from "../../test/echarts";
import ShotProfile from "./ShotProfile";
import type { ShotWindow } from "./ShotProfile";

vi.mock("./echarts", () => import("../../test/echarts"));

const WINDOWS: ShotWindow[] = [
  {
    label: "Last 10",
    zones: [
      { zone: "above_break_3", freq: 0.3, fg_pct: 0.36 },
      { zone: "rim", freq: 0.4, fg_pct: 0.65 },
    ],
  },
  {
    label: "Season",
    zones: [
      { zone: "rim", freq: 0.35, fg_pct: 0.62 },
      { zone: "short_mid", freq: 0.1, fg_pct: null },
    ],
  },
];

type Bar = { value: number | null; fgPct: number | null };
type Series = {
  type: string;
  name: string;
  data: Bar[];
  tooltip: { formatter: (params: { dataIndex: number }) => string };
};

describe("ShotProfile", () => {
  it("groups a bar per window in each zone, in court order, its height the zone's frequency", () => {
    render(<ShotProfile windows={WINDOWS} />);

    const option = lastOption();
    expect(option.xAxis).toMatchObject({
      type: "category",
      data: ["Rim", "Short mid", "Long mid", "Corner 3", "Above-break 3"],
    });
    expect(option.series).toHaveLength(2);
    expect(option.series).toMatchObject([
      {
        type: "bar",
        name: "Last 10",
        data: [{ value: 0.4 }, { value: null }, { value: null }, { value: null }, { value: 0.3 }],
      },
      {
        type: "bar",
        name: "Season",
        data: [{ value: 0.35 }, { value: 0.1 }, { value: null }, { value: null }, { value: null }],
      },
    ]);
    expect(option.legend).toMatchObject({ data: ["Last 10", "Season"] });
  });

  it("shows the zone's frequency and FG% in the tooltip", () => {
    render(<ShotProfile windows={WINDOWS} />);

    const [last10, season] = lastOption().series as Series[];
    expect(last10.tooltip.formatter({ dataIndex: 0 })).toBe("Last 10 · Rim<br>40.0% of shots, 65.0% FG");
    expect(season.tooltip.formatter({ dataIndex: 1 })).toBe("Season · Short mid<br>10.0% of shots, — FG");
  });
});

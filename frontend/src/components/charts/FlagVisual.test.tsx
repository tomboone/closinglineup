import { render } from "@testing-library/react";
import type { TooltipComponentFormatterCallbackParams } from "echarts";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../../api/types";
import { allOptions, lastOption } from "../../test/echarts";
import FlagVisual from "./FlagVisual";
import { AXIS_COLOUR, MOVER_COLOURS, TIER_COLOURS } from "./theme";

vi.mock("./echarts", () => import("../../test/echarts"));

type RaisedFlag = components["schemas"]["RaisedFlag"];

const raised = (
  flag: RaisedFlag["flag"],
  detail: RaisedFlag["detail"],
  tiers: RaisedFlag["tiers"] = null,
): RaisedFlag => ({
  game_date: "2026-03-20",
  flag,
  player: { player_id: 201950, name: "Jrue Holiday" },
  tricode: "BOS",
  detail,
  impact: null,
  tiers,
});

type BarSeries = {
  type: string;
  data: { value: number; itemStyle: { color: string } }[];
  markLine?: { data: { xAxis: number }[] };
};

function drawn(flag: RaisedFlag) {
  render(<FlagVisual raised={flag} />);
  const option = lastOption();
  return { option, series: (option.series as BarSeries[])[0]! };
}

const hover = (dataIndex: number) => {
  const formatter = (lastOption().tooltip as { formatter: (p: unknown) => string }).formatter;
  return formatter({ dataIndex } as TooltipComponentFormatterCallbackParams);
};

describe("FlagVisual", () => {
  it("draws a minutes cliff as the 5-game and 20-game shares on one scale, 60% of the 20-game marked", () => {
    const { option, series } = drawn(
      raised("minutes_cliff", { share_5: 0.05, share_20: 0.12, ratio: 0.42 }),
    );

    expect(option.yAxis).toMatchObject({ type: "category", data: ["5g", "20g"] });
    expect(series.data.map((d) => d.value)).toEqual([5, 12]);
    expect(series.data.map((d) => d.itemStyle.color)).toEqual([MOVER_COLOURS.falling, AXIS_COLOUR]);
    expect(option.xAxis).toMatchObject({ min: 0, max: 12 });
    expect(series.markLine!.data[0]!.xAxis).toBeCloseTo(7.2);
    expect(hover(0)).toBe("5g: 5.0%");
  });

  it("draws earning trust as a step line over the three 5-game shares, the oldest first", () => {
    const { option } = drawn(raised("earning_trust", { shares: [0.08, 0.1, 0.13] }));

    const [series] = option.series as { type: string; step: string; data: number[] }[];
    expect(series).toMatchObject({ type: "line", step: "middle" });
    expect(series!.data.map((v) => Number(v.toFixed(6)))).toEqual([8, 10, 13]);
    expect(option.yAxis).toMatchObject({ min: 0 });
    expect(hover(2)).toBe("Last 5 games: 13.0%");
  });

  it("draws bleeding as the on-court and off-court nets either side of zero", () => {
    const { option, series } = drawn(
      raised("bleeding", { on_court_net: -8.2, possessions: 412, off_court_net: 3.1 }),
    );

    expect(option.yAxis).toMatchObject({ data: ["On", "Off"] });
    expect(series.data).toEqual([
      { value: -8.2, itemStyle: { color: MOVER_COLOURS.falling } },
      { value: 3.1, itemStyle: { color: MOVER_COLOURS.rising } },
    ]);
    expect(option.xAxis).toMatchObject({ min: -8.2, max: 8.2 });
    expect(series.markLine!.data).toEqual([{ xAxis: 0 }]);
  });

  it("draws usage up, efficiency down as the two deltas in points, either side of zero", () => {
    const { option, series } = drawn(
      raised("usage_up_efficiency_down", {
        usg_pct_10: 0.27,
        usg_pct_delta_season: 0.04,
        ts_pct_10: 0.51,
        ts_pct_delta_season: -0.05,
      }),
    );

    expect(option.yAxis).toMatchObject({ data: ["USG", "TS"] });
    expect(series.data.map((d) => d.value)).toEqual([4, -5]);
    expect(series.data.map((d) => d.itemStyle.color)).toEqual([
      MOVER_COLOURS.rising,
      MOVER_COLOURS.falling,
    ]);
    expect(option.xAxis).toMatchObject({ min: -5, max: 5 });
    expect(hover(1)).toBe("TS: −5.0 pts");
  });

  it("draws a yo-yo as his tier strip", () => {
    const tiers = [
      { game_date: "2026-03-16", tier: "core" as const },
      { game_date: "2026-03-18", tier: null },
      { game_date: "2026-03-20", tier: "fringe" as const },
    ];
    const { series } = drawn(raised("yo_yo", { events: 3, changes: [] }, tiers));

    expect(series.data).toEqual([
      { value: 1, itemStyle: { color: TIER_COLOURS.core } },
      null,
      { value: 1, itemStyle: { color: TIER_COLOURS.fringe } },
    ]);
  });

  it("draws nothing for contract pressure, a yo-yo without tiers, or missing numbers", () => {
    const { container } = render(
      <>
        <FlagVisual raised={raised("contract_pressure", { reason: "two_way", active_games: 46, limit: 50 })} />
        <FlagVisual raised={raised("yo_yo", { events: 3, changes: [] })} />
        <FlagVisual raised={raised("minutes_cliff", { share_5: 0.05, share_20: null })} />
      </>,
    );

    expect(container).toBeEmptyDOMElement();
    expect(allOptions()).toHaveLength(0);
  });
});

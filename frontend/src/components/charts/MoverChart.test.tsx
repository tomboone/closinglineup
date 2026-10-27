import { render } from "@testing-library/react";
import type { TooltipComponentFormatterCallbackParams } from "echarts";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { click, lastOption } from "../../test/echarts";
import { percent } from "../format";
import MoverChart from "./MoverChart";
import { MOVER_COLOURS } from "./theme";

vi.mock("./echarts", () => import("../../test/echarts"));

const mover = (player_id: number, name: string, tricode: string | null, value: number, delta: number) => ({
  player: { player_id, name },
  tricode,
  value,
  delta,
  source: null,
  impact: null,
});

// Each list in the API's order, the largest move first.
const RISERS = [mover(1, "Jayson Tatum", "BOS", 0.2, 0.05), mover(2, "Josh Hart", "NYK", 0.15, 0.02)];
const FALLERS = [mover(3, "Sam Hauser", null, 0.08, -0.064), mover(4, "Miles McBride", "NYK", 0.1, -0.01)];

type Series = {
  data: { value: number; itemStyle: { color: string }; label: { position: string } }[];
  label: { formatter: string };
  markLine: { data: { xAxis: number }[] };
};

function renderChart() {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: (
          <MoverChart
            risers={RISERS}
            fallers={FALLERS}
            format={percent}
            unitLabel="Minutes share, pts"
            scale={100}
          />
        ),
      },
      { path: "/players/:id", element: null },
    ],
    { initialEntries: ["/?date=2026-03-21&tab=movers"] },
  );
  render(<RouterProvider router={router} />);
  const option = lastOption();
  return { router, option, series: (option.series as Series[])[0]! };
}

describe("MoverChart", () => {
  it("draws a bar per mover, its length the change in the unit, the largest rise first", () => {
    const { option, series } = renderChart();

    expect(option.yAxis).toMatchObject({
      type: "category",
      inverse: true,
      data: ["Jayson Tatum BOS", "Josh Hart NYK", "Miles McBride NYK", "Sam Hauser"],
    });
    expect(series.label.formatter).toBe("{b}");
    expect(series.data.map((d) => d.value)).toEqual([5, 2, -1, -6.4]);
  });

  it("colours risers and fallers apart, each name on the far side of the zero line", () => {
    const { series } = renderChart();

    expect(series.data.map((d) => d.itemStyle.color)).toEqual([
      MOVER_COLOURS.rising,
      MOVER_COLOURS.rising,
      MOVER_COLOURS.falling,
      MOVER_COLOURS.falling,
    ]);
    expect(series.data.map((d) => d.label.position)).toEqual(["left", "left", "right", "right"]);
    expect(series.markLine.data).toEqual([{ xAxis: 0 }]);
  });

  it("labels the x axis with the unit and centres it on zero, past the largest move", () => {
    const { option } = renderChart();

    expect(option.xAxis).toMatchObject({
      type: "value",
      name: "Minutes share, pts",
      min: -7.5,
      max: 7.5,
      interval: 2.5,
    });
  });

  it("shows a mover's prior and latest values on hover", () => {
    const { option } = renderChart();

    const formatter = (option.tooltip as { formatter: (p: unknown) => string }).formatter;
    const params = { dataIndex: 3 } as TooltipComponentFormatterCallbackParams;
    expect(formatter(params)).toContain("Sam Hauser<br>14.4% → 8.0%");
  });

  it("goes to the clicked mover's page, keeping the search", () => {
    const { router } = renderChart();

    click({ componentType: "series", dataIndex: 2 });

    expect(router.state.location.pathname).toBe("/players/4");
    expect(router.state.location.search).toBe("?date=2026-03-21&tab=movers");
  });
});

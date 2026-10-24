import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { allOptions, init } from "../../test/echarts";
import EChart from "./EChart";

vi.mock("./echarts", () => import("../../test/echarts"));

describe("EChart", () => {
  it("inits once, replaces the option on every change and disposes on unmount", () => {
    const first = { series: [{ type: "line" as const, data: [1] }] };
    const second = { series: [{ type: "bar" as const, data: [2] }] };

    const { rerender, unmount } = render(<EChart option={first} className="chart" />);
    rerender(<EChart option={second} className="chart" />);

    expect(init).toHaveBeenCalledTimes(1);
    expect(allOptions()).toEqual([first, second]);
    const chart = init.mock.results[0].value;
    expect(chart.setOption).toHaveBeenLastCalledWith(second, { notMerge: true });

    unmount();
    expect(chart.dispose).toHaveBeenCalledOnce();
  });
});

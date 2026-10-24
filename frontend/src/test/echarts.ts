import { vi } from "vitest";
import type { ChartOption } from "../components/charts/echarts";

// A stand-in for `components/charts/echarts`, which jsdom cannot draw: a chart test mocks that
// module with this one and reads back the option the chart set.
//
//   vi.mock("./echarts", () => import("../../test/echarts"));

const setOption = vi.fn<(option: ChartOption) => void>();

export const init = vi.fn(() => ({ setOption, resize: vi.fn(), dispose: vi.fn() }));

/** The option most recently set on any mocked chart. */
export function lastOption(): ChartOption {
  const option = setOption.mock.lastCall?.[0];
  if (!option) throw new Error("No chart has set an option");
  return option;
}

/** Every option set since the last test, in order. */
export function allOptions(): ChartOption[] {
  return setOption.mock.calls.map(([option]) => option);
}

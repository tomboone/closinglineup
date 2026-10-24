import { BarChart, CustomChart, LineChart } from "echarts/charts";
import type { BarSeriesOption, CustomSeriesOption, LineSeriesOption } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
} from "echarts/components";
import type {
  GridComponentOption,
  LegendComponentOption,
  MarkAreaComponentOption,
  MarkLineComponentOption,
  MarkPointComponentOption,
  TooltipComponentOption,
} from "echarts/components";
import { init, use as register } from "echarts/core";
import type { ComposeOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

// The one place ECharts is imported (spec §9: the only charting library). Registering just the
// series and components the charts draw keeps the rest of ECharts out of the bundle, and tests mock
// this module's `init` to read the option a chart sets.

// `use` renamed, as the linter takes a top-level `use()` for React's hook.
register([
  BarChart,
  CustomChart,
  LineChart,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
  CanvasRenderer,
]);

export type ChartOption = ComposeOption<
  | BarSeriesOption
  | CustomSeriesOption
  | LineSeriesOption
  | GridComponentOption
  | LegendComponentOption
  | MarkAreaComponentOption
  | MarkLineComponentOption
  | MarkPointComponentOption
  | TooltipComponentOption
>;

export { init };

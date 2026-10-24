import { render } from "@testing-library/react";
import type { TooltipComponentFormatterCallbackParams } from "echarts";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../../api/types";
import { lastOption } from "../../test/echarts";
import AnnotatedTimeSeries from "./AnnotatedTimeSeries";
import { ANNOTATION_COLOURS } from "./theme";

vi.mock("./echarts", () => import("../../test/echarts"));

type Annotation = components["schemas"]["Annotation"];

const SERIES = [
  {
    name: "Minutes",
    points: [
      { game_date: "2026-03-14", value: 31.5 },
      { game_date: "2026-03-16", value: 28 },
      { game_date: "2026-03-18", value: 12.25 },
    ],
  },
  {
    name: "Share",
    points: [
      { game_date: "2026-03-16", value: 14 },
      { game_date: "2026-03-18", value: null },
    ],
  },
];

const ANNOTATIONS: Annotation[] = [
  { game_date: "2026-03-16", kind: "starter", label: "Moved to the bench" },
  { game_date: "2026-03-18", kind: "injury", label: "Questionable → Out (ankle)" },
  { game_date: "2026-03-18", kind: "transaction", label: "Signed <two-way>" },
];

const minutes = (value: number) => `${value.toFixed(1)} min`;

type Series = {
  type: string;
  name: string;
  data: unknown[];
  markLine?: { data: MarkLineDatum[] };
  markPoint?: { data: PinDatum[] };
};
type PinDatum = { coord: [string, number]; name: string; symbolOffset: [number, number]; itemStyle: { color: string } };
type MarkLineDatum = { xAxis: string; name: string; lineStyle: { color: string }; label: { formatter: string } };

function renderChart(annotations = ANNOTATIONS) {
  render(<AnnotatedTimeSeries series={SERIES} annotations={annotations} yFormat={minutes} />);
  const option = lastOption();
  return { option, series: option.series as Series[] };
}

describe("AnnotatedTimeSeries", () => {
  it("draws a line per series on the union of their dates, a gap where one has no value", () => {
    const { option, series } = renderChart([]);

    expect(option.xAxis).toMatchObject({ data: ["2026-03-14", "2026-03-16", "2026-03-18"] });
    expect(series).toHaveLength(2);
    expect(series).toMatchObject([
      { type: "line", name: "Minutes", data: [31.5, 28, 12.25] },
      { type: "line", name: "Share", data: [null, 14, null] },
    ]);
    expect(option.legend).toMatchObject({ data: ["Minutes", "Share"] });
  });

  it("places one marker per annotation in its kind's colour, labelled", () => {
    const { series } = renderChart();

    const markers = series.flatMap((s) => s.markLine?.data ?? []);
    expect(markers).toHaveLength(ANNOTATIONS.length);
    expect(markers).toMatchObject(
      ANNOTATIONS.map((a) => ({
        xAxis: a.game_date,
        name: a.label,
        lineStyle: { color: ANNOTATION_COLOURS[a.kind] },
        label: { formatter: a.label },
      })),
    );
  });

  it("pins each annotation above the highest value, stacking those that share a date", () => {
    const { series } = renderChart();

    const pins = series.flatMap((s) => s.markPoint?.data ?? []);
    expect(pins).toEqual(
      ANNOTATIONS.map((a, i) => ({
        coord: [a.game_date, 31.5],
        name: a.label,
        symbolOffset: [0, [0, 0, -12][i]],
        itemStyle: { color: ANNOTATION_COLOURS[a.kind] },
      })),
    );
  });

  it("puts an annotation on a date no series has a point for on the axis", () => {
    const { option } = renderChart([
      { game_date: "2026-03-15", kind: "transaction", label: "Assigned to G League" },
    ]);

    expect(option.xAxis).toMatchObject({
      data: ["2026-03-14", "2026-03-15", "2026-03-16", "2026-03-18"],
    });
  });

  it("formats values with yFormat and lists the date's annotations in the tooltip, escaped", () => {
    const { option } = renderChart();

    const tooltip = option.tooltip as {
      formatter: (params: TooltipComponentFormatterCallbackParams) => string;
    };
    const html = tooltip.formatter([
      { seriesName: "Minutes", name: "2026-03-18", value: 12.25, marker: "" },
      { seriesName: "Share", name: "2026-03-18", value: null, marker: "" },
    ] as unknown as TooltipComponentFormatterCallbackParams);

    expect(html).toContain("2026-03-18");
    expect(html).toContain("Minutes: 12.3 min");
    expect(html).toContain("Share: —");
    expect(html).toContain("Questionable → Out (ankle)");
    expect(html).toContain("Signed &lt;two-way&gt;");
    expect(html).not.toContain("Moved to the bench");
  });
});

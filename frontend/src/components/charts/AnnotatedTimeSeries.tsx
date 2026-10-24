import type { TooltipComponentFormatterCallbackParams } from "echarts";
import { useMemo } from "react";
import type { components } from "../../api/types";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { ANNOTATION_COLOURS, SERIES_COLOURS } from "./theme";
import { dot, escapeHtml } from "./tooltip";

type Schemas = components["schemas"];

export type NamedSeries = { name: string; points: Schemas["Point"][] };

const PIN_GAP = 12;

// The series that carries the annotations' markers, so hiding a data series keeps them.
const EVENTS = "Events";

/**
 * Lines over game dates, one per series, with a vertical line and a pin per annotation in its
 * kind's colour; the axis tooltip lists each series' value through `yFormat` and the date's annotations.
 */
function AnnotatedTimeSeries({
  series,
  annotations,
  yFormat,
}: {
  series: NamedSeries[];
  annotations: Schemas["Annotation"][];
  yFormat: (value: number) => string;
}) {
  const option = useMemo(
    () => annotatedTimeSeriesOption(series, annotations, yFormat),
    [series, annotations, yFormat],
  );
  return <EChart option={option} className="chart" />;
}

function annotatedTimeSeriesOption(
  series: NamedSeries[],
  annotations: Schemas["Annotation"][],
  yFormat: (value: number) => string,
): ChartOption {
  const dates = [
    ...new Set([
      ...series.flatMap((s) => s.points.map((point) => point.game_date)),
      ...annotations.map((a) => a.game_date),
    ]),
  ].sort();

  // A pin per annotation above the highest value, stacked upwards when several share a date, so
  // one whose line another covers still shows its colour. No values, no height to pin them at.
  const allValues = series.flatMap((s) => s.points.flatMap((point) => point.value ?? []));
  const pins =
    allValues.length === 0
      ? []
      : annotations.map((a, i) => {
          const below = annotations.slice(0, i).filter((b) => b.game_date === a.game_date).length;
          return {
            coord: [a.game_date, Math.max(...allValues)],
            name: a.label,
            symbolOffset: [0, below === 0 ? 0 : -PIN_GAP * below],
            itemStyle: { color: ANNOTATION_COLOURS[a.kind] },
          };
        });

  const formatTooltip = (params: TooltipComponentFormatterCallbackParams) => {
    const items = (Array.isArray(params) ? params : [params]).filter(
      (item) => item.seriesName !== EVENTS,
    );
    const date = items[0]?.name ?? "";
    const values = items.map((item) => {
      const marker = typeof item.marker === "string" ? item.marker : "";
      const value = typeof item.value === "number" ? escapeHtml(yFormat(item.value)) : "—";
      return `${marker}${escapeHtml(item.seriesName ?? "")}: ${value}`;
    });
    const events = annotations
      .filter((a) => a.game_date === date)
      .map((a) => `${dot(ANNOTATION_COLOURS[a.kind])}${escapeHtml(a.label)}`);
    return [escapeHtml(date), ...values, ...events].join("<br>");
  };

  return {
    color: SERIES_COLOURS,
    grid: { top: 64, right: 24, bottom: 32, left: 56 },
    legend: { top: 0, data: series.map((s) => s.name) },
    tooltip: { trigger: "axis", formatter: formatTooltip },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", scale: true, axisLabel: { formatter: yFormat } },
    series: [
      ...series.map((s) => {
        const byDate = new Map(s.points.map((point) => [point.game_date, point.value]));
        return {
          type: "line" as const,
          name: s.name,
          data: dates.map((date) => byDate.get(date) ?? null),
          connectNulls: false,
          symbolSize: 5,
        };
      }),
      ...(annotations.length > 0
        ? [
            {
              type: "line" as const,
              name: EVENTS,
              data: [],
              markLine: {
                symbol: "none",
                animation: false,
                data: annotations.map((a) => ({
                  xAxis: a.game_date,
                  name: a.label,
                  lineStyle: { color: ANNOTATION_COLOURS[a.kind], type: "dashed" as const },
                  label: { show: false, formatter: a.label, color: ANNOTATION_COLOURS[a.kind] },
                  emphasis: { label: { show: true } },
                })),
              },
              ...(pins.length > 0 && {
                markPoint: {
                  symbol: "circle",
                  symbolSize: 9,
                  animation: false,
                  label: { show: false },
                  data: pins,
                },
              }),
            },
          ]
        : []),
    ],
  };
}

export default AnnotatedTimeSeries;

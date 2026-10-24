import { useMemo } from "react";
import type { components } from "../../api/types";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { SERIES_COLOURS } from "./theme";
import { escapeHtml } from "./tooltip";

type ShotZone = components["schemas"]["ShotZone"];

/** One span's shot profile (a window, the season, the prior season): each zone it has a row for. */
export type ShotWindow = { label: string; zones: Pick<ShotZone, "zone" | "freq" | "fg_pct">[] };

// From the rim out, so the bars read as a walk away from the basket.
const ZONES: { zone: ShotZone["zone"]; label: string }[] = [
  { zone: "rim", label: "Rim" },
  { zone: "short_mid", label: "Short mid" },
  { zone: "long_mid", label: "Long mid" },
  { zone: "corner_3", label: "Corner 3" },
  { zone: "above_break_3", label: "Above-break 3" },
];

function percent(value: number | null) {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

/** Grouped bars of each zone's share of attempts, a bar per window, FG% in the tooltip. */
function ShotProfile({ windows }: { windows: ShotWindow[] }) {
  const option = useMemo(() => shotProfileOption(windows), [windows]);
  return <EChart option={option} className="chart" />;
}

function shotProfileOption(windows: ShotWindow[]): ChartOption {
  return {
    color: SERIES_COLOURS,
    grid: { top: 36, right: 24, bottom: 32, left: 56 },
    legend: { top: 0, data: windows.map((w) => w.label) },
    tooltip: { trigger: "item" },
    xAxis: { type: "category", data: ZONES.map((z) => z.label) },
    yAxis: { type: "value", axisLabel: { formatter: (value: number) => percent(value) } },
    series: windows.map((window) => {
      const zones = ZONES.map(({ zone }) => window.zones.find((z) => z.zone === zone));
      return {
        type: "bar" as const,
        name: window.label,
        data: zones.map((z) => ({ value: z?.freq ?? null })),
        tooltip: {
          formatter: ({ dataIndex }: { dataIndex: number }) =>
            `${escapeHtml(window.label)} · ${ZONES[dataIndex].label}<br>` +
            `${percent(zones[dataIndex]?.freq ?? null)} of shots, ${percent(zones[dataIndex]?.fg_pct ?? null)} FG`,
        },
      };
    }),
  };
}

export default ShotProfile;

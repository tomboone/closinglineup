import type { TooltipComponentFormatterCallbackParams } from "echarts";
import { useMemo } from "react";
import type { components } from "../../api/types";
import { TIER_LABELS } from "../labels";
import EChart from "./EChart";
import type { ChartOption } from "./echarts";
import { TIER_COLOURS } from "./theme";
import { dot, escapeHtml } from "./tooltip";

type TierPoint = components["schemas"]["TierPoint"];

/**
 * A player's tier over a run of his team's game dates: a cell per date, the oldest first, each in
 * its tier's colour, a gap for a date he had no row on. Hovering a cell gives its date and tier.
 * Flag-row sized unless `className` sizes it otherwise.
 */
function TierStrip({ tiers, className = "chart-flag" }: { tiers: TierPoint[]; className?: string }) {
  const option = useMemo(() => stripOption(tiers), [tiers]);
  return <EChart option={option} className={className} />;
}

function stripOption(tiers: TierPoint[]): ChartOption {
  const formatTooltip = (params: TooltipComponentFormatterCallbackParams) => {
    const item = Array.isArray(params) ? params[0] : params;
    const point = item === undefined ? undefined : tiers[item.dataIndex];
    if (point?.tier == null) return "";
    return (
      `${dot(TIER_COLOURS[point.tier])}${escapeHtml(point.game_date)}<br>` +
      escapeHtml(TIER_LABELS[point.tier])
    );
  };

  return {
    animation: false,
    grid: { top: 8, right: 2, bottom: 8, left: 2 },
    // On the page's body, as the strip is too small to hold it.
    tooltip: { trigger: "item", formatter: formatTooltip, appendTo: "body" },
    xAxis: { type: "category", show: false, data: tiers.map((point) => point.game_date) },
    yAxis: { type: "value", show: false, min: 0, max: 1 },
    series: [
      {
        type: "bar",
        barCategoryGap: "15%",
        // Every cell is the strip's full height; only its colour carries the tier.
        data: tiers.map(({ tier }) =>
          tier === null ? null : { value: 1, itemStyle: { color: TIER_COLOURS[tier] } },
        ),
      },
    ],
  };
}

export default TierStrip;

import { render } from "@testing-library/react";
import type { TooltipComponentFormatterCallbackParams } from "echarts";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../../api/types";
import { lastOption } from "../../test/echarts";
import { TIER_COLOURS } from "./theme";
import TierStrip from "./TierStrip";

vi.mock("./echarts", () => import("../../test/echarts"));

type Tier = components["schemas"]["Tier"];

const CYCLE: Tier[] = ["starter", "core", "fringe", "out", "injured"];
const DATES = Array.from({ length: 20 }, (_, i) => `2026-03-${String(i + 1).padStart(2, "0")}`);
// 18 of the 20 dates with a row: none on the 4th or the 12th.
const MISSING = new Set([3, 11]);
const TIERS = DATES.map((game_date, i) => ({
  game_date,
  tier: MISSING.has(i) ? null : CYCLE[i % CYCLE.length]!,
}));

type Series = { data: ({ value: number; itemStyle: { color: string } } | null)[] };

describe("TierStrip", () => {
  it("draws a cell per date, the oldest first, each in its tier's colour, a gap where he had no row", () => {
    render(<TierStrip tiers={TIERS} />);

    const option = lastOption();
    expect(option.xAxis).toMatchObject({ type: "category", show: false, data: DATES });
    const { data } = (option.series as Series[])[0]!;
    expect(data).toHaveLength(20);
    expect(data.map((cell) => cell?.itemStyle.color ?? null)).toEqual(
      TIERS.map(({ tier }) => (tier === null ? null : TIER_COLOURS[tier])),
    );
    expect(data[3]).toBeNull();
    expect(data[11]).toBeNull();
  });

  it("gives a cell's date and tier on hover", () => {
    render(<TierStrip tiers={TIERS} />);

    const formatter = (lastOption().tooltip as { formatter: (p: unknown) => string }).formatter;
    expect(formatter({ dataIndex: 2 } as TooltipComponentFormatterCallbackParams)).toContain(
      "2026-03-03<br>Fringe",
    );
  });
});

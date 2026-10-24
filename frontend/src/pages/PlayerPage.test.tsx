import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChartOption } from "../components/charts/echarts";
import { ANNOTATION_COLOURS } from "../components/charts/theme";
import { mockApi, PLAYER_PAGE, renderApp } from "../test/app";
import { allOptions } from "../test/echarts";

vi.mock("../components/charts/echarts", () => import("../test/echarts"));

type Series = { name: string; markLine?: { data: { xAxis: string; name: string; lineStyle: { color: string } }[] } };

const PATH = "/api/players/1627759";

/** The options set by the annotated time series, told apart from the shot profile by their axis. */
function timeSeriesOptions(): ChartOption[] {
  return allOptions().filter((option) =>
    (option.series as { type: string }[]).every((series) => series.type === "line"),
  );
}

describe("PlayerPage", () => {
  it("shows the header with his team, tier and window", async () => {
    mockApi({ [PATH]: { body: PLAYER_PAGE } });
    renderApp("/players/1627759?date=2026-03-20");

    expect(await screen.findByRole("heading", { level: 1, name: "Jaylen Brown" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "BOS" })).toHaveAttribute("href", "/teams/BOS?date=2026-03-20");
    expect(screen.getByText("Starter")).toBeInTheDocument();
    expect(screen.getByLabelText("Last 10")).toBeChecked();
  });

  it("marks the injury and the transaction on every time series, at their dates", async () => {
    mockApi({ [PATH]: { body: PLAYER_PAGE } });
    renderApp("/players/1627759");
    await screen.findByRole("heading", { level: 1, name: "Jaylen Brown" });

    const charts = timeSeriesOptions();
    expect(charts).toHaveLength(4);
    for (const option of charts) {
      const markers = (option.series as Series[]).flatMap((s) => s.markLine?.data ?? []);
      expect(markers).toEqual([
        expect.objectContaining({
          xAxis: "2026-03-11",
          name: "Available → Questionable (knee)",
          lineStyle: expect.objectContaining({ color: ANNOTATION_COLOURS.injury }),
        }),
        expect.objectContaining({
          xAxis: "2026-03-12",
          name: "Signed extension",
          lineStyle: expect.objectContaining({ color: ANNOTATION_COLOURS.transaction }),
        }),
      ]);
    }
  });

  it("refetches with window= and redraws the series when the window is switched", async () => {
    const fetchMock = mockApi({ [PATH]: { body: PLAYER_PAGE } });
    renderApp("/players/1627759");
    await screen.findByRole("heading", { level: 1, name: "Jaylen Brown" });
    expect(fetchMock).toHaveBeenCalledWith(`${PATH}?garbage_time=exclude`);

    const lastFive = {
      ...PLAYER_PAGE,
      window: "5" as const,
      series: { ...PLAYER_PAGE.series, minutes: [{ game_date: "2026-03-12", value: 36 }] },
    };
    const refetch = mockApi({ [PATH]: { body: lastFive } });
    fireEvent.click(screen.getByLabelText("Last 5"));

    await waitFor(() => expect(refetch).toHaveBeenCalledWith(`${PATH}?garbage_time=exclude&window=5`));
    await waitFor(() => {
      const minutes = timeSeriesOptions()
        .map((option) => (option.series as { name: string; data: unknown[] }[])[0])
        .filter((series) => series.name === "Minutes")
        .at(-1);
      expect(minutes?.data).toEqual([null, null, 36]);
    });
    expect(screen.getByLabelText("Last 5")).toBeChecked();
  });

  it("links each teammate and matchup to his player page, keeping the date", async () => {
    mockApi({ [PATH]: { body: PLAYER_PAGE } });
    renderApp("/players/1627759?date=2026-03-20");

    const teammates = await screen.findByRole("region", { name: "Teammates" });
    expect(within(teammates).getByRole("link", { name: "Jayson Tatum" })).toHaveAttribute(
      "href",
      "/players/1628369?date=2026-03-20",
    );
    expect(within(teammates).getByRole("link", { name: "Derrick White" })).toHaveAttribute(
      "href",
      "/players/1628401?date=2026-03-20",
    );
    expect(within(teammates).getByText("+9.1")).toBeInTheDocument();

    const matchups = screen.getByRole("region", { name: "Matchups" });
    expect(within(matchups).getByRole("link", { name: "OG Anunoby" })).toHaveAttribute(
      "href",
      "/players/1628384?date=2026-03-20",
    );
    expect(within(matchups).getByText("5-11")).toBeInTheDocument();
    expect(within(matchups).getByText("None in the window.")).toBeInTheDocument();
  });

  it("lists the season's flags with what raised them", async () => {
    mockApi({ [PATH]: { body: PLAYER_PAGE } });
    renderApp("/players/1627759");

    const flags = await screen.findByRole("region", { name: "Flag history" });
    expect(within(flags).getByText("Minutes cliff")).toBeInTheDocument();
    expect(within(flags).getByText("5-game share 8.0% vs 20-game 14.0%")).toBeInTheDocument();
  });

  it("says so when no impact metric has a value, still charting the rest", async () => {
    mockApi({ [PATH]: { body: { ...PLAYER_PAGE, series: { ...PLAYER_PAGE.series, impact: [] } } } });
    renderApp("/players/1627759");

    const impact = await screen.findByRole("region", { name: "Impact" });
    expect(within(impact).getByText("No impact metric has a value for him yet.")).toBeInTheDocument();
    expect(timeSeriesOptions()).toHaveLength(3);
  });
});

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CLOSE_GAME_PAGE, GAME_PAGE, mockApi, renderApp } from "../test/app";
import { lastOption } from "../test/echarts";

vi.mock("../components/charts/echarts", () => import("../test/echarts"));

type Series = {
  type: string;
  name: string;
  data?: { value: number[] }[];
  markArea?: { data: [{ xAxis: number }, { xAxis: number }][] };
};

describe("GamePage", () => {
  it("draws a timeline row per player, away team first, with garbage time shaded", async () => {
    mockApi({ "/api/games/0022500990": { body: GAME_PAGE } });
    renderApp("/games/0022500990");
    await screen.findByRole("heading", { name: "NYK at BOS" });
    expect(screen.getByText(/^Final, 2026-03-19:/)).toHaveTextContent(
      "Final, 2026-03-19: NYK 101, BOS 126.",
    );

    const option = lastOption();
    expect(option.yAxis).toMatchObject({
      data: [
        "Jalen Brunson",
        "Josh Hart",
        "OG Anunoby",
        "Mikal Bridges",
        "Karl-Anthony Towns",
        "Miles McBride",
        "Jayson Tatum",
        "Jaylen Brown",
        "Derrick White",
        "Jrue Holiday",
        "Kristaps Porzingis",
        "JD Davison",
      ],
    });
    const series = option.series as Series[];
    expect(series.filter((s) => s.type === "custom").map((s) => s.name)).toEqual(["NYK", "BOS"]);
    // Tatum's two stints sit on his row, the seventh; Q4 5:00 left is 2580 s since tip-off.
    const bos = series.find((s) => s.name === "BOS")!;
    expect(bos.data!.filter((d) => d.value[2] === 6).map((d) => d.value.slice(0, 2))).toEqual([
      [0, 540],
      [1440, 2160],
    ]);
    const areas = series.flatMap((s) => s.markArea?.data ?? []);
    expect(areas).toEqual([[expect.objectContaining({ xAxis: 2580 }), { xAxis: 2880 }]]);
  });

  it("says a game decided by more than 10 was not close", async () => {
    mockApi({ "/api/games/0022500990": { body: GAME_PAGE } });
    renderApp("/games/0022500990");

    const closing = await screen.findByRole("region", { name: "Closing lineup" });
    expect(within(closing).getByText("Not a close game.")).toBeInTheDocument();
  });

  it("shows each team's closing five in a close game, with no garbage time shaded", async () => {
    mockApi({ "/api/games/0022500990": { body: CLOSE_GAME_PAGE } });
    renderApp("/games/0022500990");

    const close = await screen.findByRole("region", { name: "Closing lineup" });
    expect((lastOption().series as Series[]).some((s) => s.markArea)).toBe(false);
    expect(within(close).queryByText("Not a close game.")).not.toBeInTheDocument();
    expect(within(close).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Jalen Brunson",
      "Josh Hart",
      "OG Anunoby",
      "Mikal Bridges",
      "Miles McBride",
      "Jayson Tatum",
      "Jaylen Brown",
      "Derrick White",
      "Jrue Holiday",
      "Kristaps Porzingis",
    ]);
  });

  it("links every timeline row and lineup player to their player page", async () => {
    mockApi({ "/api/games/0022500990": { body: GAME_PAGE } });
    renderApp("/games/0022500990?date=2026-03-20");
    const stints = await screen.findByRole("region", { name: "Stints" });

    const rows = within(stints).getAllByRole("link");
    expect(rows).toHaveLength(12);
    expect(rows.every((link) => link.getAttribute("href")!.startsWith("/players/"))).toBe(true);
    expect(within(stints).getByRole("link", { name: "Jayson Tatum" })).toHaveAttribute(
      "href",
      "/players/1628369?date=2026-03-20",
    );
    const celtics = within(stints).getByRole("table", { name: "BOS" });
    expect(within(celtics).getByText("33:30")).toBeInTheDocument();

    const lineups = screen.getByRole("region", { name: "Lineups" });
    expect(within(lineups).getAllByRole("link")).toHaveLength(10);
    expect(within(lineups).getByText("+19.2")).toBeInTheDocument();
  });

  it("refetches with garbage_time=include when garbage time is toggled on", async () => {
    const fetchMock = mockApi({ "/api/games/0022500990": { body: GAME_PAGE } });
    renderApp("/games/0022500990");
    await screen.findByRole("heading", { name: "NYK at BOS" });

    fireEvent.click(screen.getByLabelText("Include garbage time"));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/games/0022500990?garbage_time=include"),
    );
  });
});

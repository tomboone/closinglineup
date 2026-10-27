import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { mockApi, renderApp, TEAM_PAGE } from "../test/app";
import { allOptions } from "../test/echarts";

vi.mock("../components/charts/echarts", () => import("../test/echarts"));

describe("TeamPage", () => {
  it("groups players under their tiers in precedence order, with counts and badges", async () => {
    mockApi({ "/api/teams/BOS": { body: TEAM_PAGE } });
    renderApp("/teams/BOS");
    await screen.findByText("Boston Celtics, as of 2026-03-19.");

    const rotation = screen.getByRole("region", { name: "Rotation" });
    const headings = within(rotation)
      .getAllByText(/^(Injured|Starter|Core|Fringe|Out) \(\d+\)$/)
      .map((heading) => heading.textContent);
    expect(headings).toEqual(["Injured (1)", "Starter (2)", "Core (1)", "Out (1)"]);

    const names = (tier: string) =>
      within(within(rotation).getByRole("rowgroup", { name: tier }))
        .getAllByRole("link")
        .map((link) => link.textContent);
    expect(names("Injured (1)")).toEqual(["Kristaps Porzingis"]);
    expect(names("Starter (2)")).toEqual(["Jayson Tatum", "Jaylen Brown"]);
    expect(names("Core (1)")).toEqual(["Payton Pritchard"]);
    expect(names("Out (1)")).toEqual(["JD Davison"]);

    const out = within(rotation).getByRole("rowgroup", { name: "Out (1)" });
    expect(within(out).getByText("Two-way")).toBeInTheDocument();
    expect(within(out).getByText("G League")).toBeInTheDocument();
    const core = within(rotation).getByRole("rowgroup", { name: "Core (1)" });
    expect(within(core).getByText("short window")).toBeInTheDocument();

    // Three sparklines a player.
    expect(allOptions()).toHaveLength(3 * TEAM_PAGE.tier_board.length);
    expect(screen.getByText("Roster: 14 of 15 standard, 3 of 3 two-way.")).toBeInTheDocument();
  });

  it("gives every sparkline in a column one y range and prints it in the column's header", async () => {
    mockApi({ "/api/teams/BOS": { body: TEAM_PAGE } });
    renderApp("/teams/BOS");
    const rotation = await screen.findByRole("region", { name: "Rotation" });

    // Minutes share runs 10–12%; on/off net −1 to +4 and impact 1.1 to 1.3, each widened to its
    // zero baseline and rounded out to a step. The rows' impact sources differ, so it is "Impact".
    const headers = within(rotation)
      .getAllByRole("columnheader")
      .map((header) => header.textContent);
    expect(headers.slice(3)).toEqual([
      "Min share (10–12%)",
      "On/off net (−1 to +4)",
      "Impact (0 to +1.4)",
    ]);

    type Line = { endLabel: { formatter: () => string } };
    type YAxis = { min: (r: { min: number }) => number; max: (r: { max: number }) => number };
    const columns = [0, 1, 2].map((column) => allOptions().filter((_, i) => i % 3 === column));
    const ranges = columns.map((options) =>
      options.map((option) => {
        const yAxis = option.yAxis as YAxis;
        return [yAxis.min({ min: -99 }), yAxis.max({ max: 99 })];
      }),
    );
    expect(ranges[0]).toEqual(Array(TEAM_PAGE.tier_board.length).fill([0.1, 0.12]));
    expect(ranges[1]).toEqual(Array(TEAM_PAGE.tier_board.length).fill([-1, 4]));
    expect(ranges[2]).toEqual(Array(TEAM_PAGE.tier_board.length).fill([0, 1.4]));

    // Each line ends with its latest value in its column's format.
    const endLabels = columns.map((options) => (options[0].series as Line[])[0].endLabel.formatter());
    expect(endLabels).toEqual(["12.0%", "+4.0", "+1.3"]);
  });

  it("rounds a column of one value out to a range with a height", async () => {
    const page = {
      ...TEAM_PAGE,
      tier_board: TEAM_PAGE.tier_board.map((row) => ({
        ...row,
        minutes_share: [{ game_date: "2026-03-19", value: 0.1234567 }],
        on_off_net: [{ game_date: "2026-03-19", value: 0 }],
      })),
    };
    mockApi({ "/api/teams/BOS": { body: page } });
    renderApp("/teams/BOS");
    const rotation = await screen.findByRole("region", { name: "Rotation" });

    expect(within(rotation).getByRole("columnheader", { name: "Min share (12–14%)" })).toBeInTheDocument();
    expect(
      within(rotation).getByRole("columnheader", { name: "On/off net (0 to +0.1)" }),
    ).toBeInTheDocument();
  });

  it("names the impact column for its source when every row shares one", async () => {
    const page = {
      ...TEAM_PAGE,
      tier_board: TEAM_PAGE.tier_board.map((row) => ({
        ...row,
        impact_source: "darko" as const,
        impact: [
          { game_date: "2026-03-18", value: -3.6 },
          { game_date: "2026-03-19", value: 5.2 },
        ],
      })),
    };
    mockApi({ "/api/teams/BOS": { body: page } });
    renderApp("/teams/BOS");
    const rotation = await screen.findByRole("region", { name: "Rotation" });

    expect(
      within(rotation).getByRole("columnheader", { name: "DARKO (−4 to +6)" }),
    ).toBeInTheDocument();
  });

  it("links every name to its player page and every closing lineup to its game page", async () => {
    mockApi({ "/api/teams/BOS": { body: TEAM_PAGE } });
    renderApp("/teams/BOS?date=2026-03-20");
    await screen.findByText("Boston Celtics, as of 2026-03-19.");

    const closing = screen.getByRole("region", { name: "Closing lineups" });
    expect(within(closing).getByRole("link", { name: "2026-03-19 vs NYK" })).toHaveAttribute(
      "href",
      "/games/0022500990?date=2026-03-20",
    );
    expect(within(closing).getByText("W +4")).toBeInTheDocument();

    for (const region of ["Rotation", "Availability", "Closing lineups", "Top lineups", "Contract notes"]) {
      const links = within(screen.getByRole("region", { name: region })).getAllByRole("link");
      const playerLinks = links.filter((link) => link.getAttribute("href")!.startsWith("/players/"));
      expect(playerLinks.length).toBeGreaterThan(0);
    }
    expect(screen.getAllByRole("link", { name: "Jayson Tatum" })[0]).toHaveAttribute(
      "href",
      "/players/1628369?date=2026-03-20",
    );
  });

  it("marks the availability board stale when the report is ESPN's", async () => {
    const page = {
      ...TEAM_PAGE,
      availability: { ...TEAM_PAGE.availability, source: "espn", stale: true },
    };
    mockApi({ "/api/teams/BOS": { body: page } });
    renderApp("/teams/BOS");

    const availability = await screen.findByRole("region", { name: "Availability" });
    expect(within(availability).getByText("stale")).toBeInTheDocument();
    expect(within(availability).getByText(/^ESPN,/)).toBeInTheDocument();
  });

  it("refetches with garbage_time=include when garbage time is toggled on", async () => {
    const fetchMock = mockApi({ "/api/teams/BOS": { body: TEAM_PAGE } });
    renderApp("/teams/BOS");
    await screen.findByText("Boston Celtics, as of 2026-03-19.");

    fireEvent.click(screen.getByLabelText("Include garbage time"));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/teams/BOS?garbage_time=include"),
    );
  });
});

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

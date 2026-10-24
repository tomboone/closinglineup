import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { mockApi, renderApp, TEAM_PAGE } from "./test/app";
import { allOptions } from "./test/echarts";

vi.mock("./components/charts/echarts", () => import("./test/echarts"));

const PAGES = [
  {
    path: "/",
    api: "/api/league",
    heading: "League",
    body: { date: "2026-03-20" },
    content: "Morning feed for 2026-03-20.",
  },
  {
    path: "/teams/BOS",
    api: "/api/teams/BOS",
    heading: "Team BOS",
    body: TEAM_PAGE,
    content: "Boston Celtics, as of 2026-03-19.",
  },
  {
    path: "/players/1628369",
    api: "/api/players/1628369",
    heading: "Player",
    body: { date: "2026-03-20", header: { player: { player_id: 1628369, name: "Jayson Tatum" } } },
    content: "Jayson Tatum, as of 2026-03-20.",
  },
  {
    path: "/games/0022500001",
    api: "/api/games/0022500001",
    heading: "Game 0022500001",
    body: { game_date: "2025-10-21", away: { tricode: "NYK" }, home: { tricode: "BOS" } },
    content: "NYK at BOS, 2025-10-21.",
  },
];

describe("routes", () => {
  it.each(PAGES)("$path shows its loading state", async ({ path, api, heading }) => {
    mockApi({ [api]: "pending" });
    renderApp(path);

    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
  });

  it.each(PAGES)("$path shows its error state", async ({ path, api }) => {
    mockApi({ [api]: { status: 404, body: { detail: "No such thing" } } });
    renderApp(path);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't load this page: No such thing",
    );
  });

  it.each(PAGES)("$path renders its data", async ({ path, api, body, content }) => {
    mockApi({ [api]: { body } });
    renderApp(path);

    expect(await screen.findByText(content)).toBeInTheDocument();
  });
});

describe("/dev/charts", () => {
  it("renders every chart with sample data in dev, needing no API", async () => {
    const fetchMock = mockApi();
    renderApp("/dev/charts");

    expect(await screen.findByRole("heading", { name: "Charts" })).toBeInTheDocument();
    for (const chart of ["SparklineCell", "AnnotatedTimeSeries", "StintTimeline", "ShotProfile"]) {
      expect(screen.getByRole("heading", { name: chart })).toBeInTheDocument();
    }
    // Three sparklines and one of each other chart.
    expect(allOptions()).toHaveLength(6);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

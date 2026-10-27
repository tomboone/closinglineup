import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import {
  GAME_PAGE,
  LEAGUE_PAGE,
  mockApi,
  PLAYER_PAGE,
  renderApp,
  SLATE_PAGE,
  TEAM_PAGE,
} from "./test/app";
import { allOptions } from "./test/echarts";

vi.mock("./components/charts/echarts", () => import("./test/echarts"));

const PAGES = [
  {
    path: "/?date=2026-03-21",
    api: "/api/slate",
    heading: "League",
    body: SLATE_PAGE,
    content: "Today: 2026-03-21",
  },
  {
    path: "/teams/BOS",
    api: "/api/teams/BOS",
    heading: "Team BOS",
    body: TEAM_PAGE,
    content: "Boston Celtics, as of 2026-03-19.",
  },
  {
    path: "/players/1627759",
    api: "/api/players/1627759",
    heading: "Player",
    body: PLAYER_PAGE,
    content: "as of 2026-03-20",
  },
  {
    path: "/games/0022500990",
    api: "/api/games/0022500990",
    heading: "Game",
    body: GAME_PAGE,
    content: "Not a close game.",
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

describe("/ (the feed)", () => {
  const morning = () =>
    mockApi({ "/api/slate": { body: SLATE_PAGE }, "/api/league": { body: LEAGUE_PAGE } });

  it("opens the tab in ?tab= and the teams in ?team=, so a morning is a URL", async () => {
    morning();
    renderApp("/?date=2026-03-21&tab=flags&team=LAL,BOS");

    expect(await screen.findByRole("tab", { name: "Flags" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const flags = await screen.findByRole("region", { name: "Flags" });
    expect(within(flags).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("Teams: BOS, LAL")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "BOS" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "LAL" })).toBeChecked();
  });

  it("filters to ?team=, a team with no rows showing none", async () => {
    morning();
    renderApp("/?date=2026-03-21&tab=flags&team=LAL");

    expect(await screen.findByRole("region", { name: "Flags" })).toHaveTextContent(
      "No flags raised.",
    );
  });

  it("opens on Today when ?tab= is absent or unknown", async () => {
    morning();
    renderApp("/?date=2026-03-21&tab=standings");

    expect(await screen.findByRole("tab", { name: "Today" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("keeps ?tab= and ?team= on a link off the feed and back", async () => {
    mockApi({
      "/api/slate": { body: SLATE_PAGE },
      "/api/league": { body: LEAGUE_PAGE },
      "/api/teams/BOS": { body: TEAM_PAGE },
    });
    const router = renderApp("/?date=2026-03-21&tab=flags&team=BOS");
    const flags = await screen.findByRole("region", { name: "Flags" });

    fireEvent.click(within(flags).getAllByRole("link", { name: "BOS" })[0]!);
    expect(router.state.location.pathname).toBe("/teams/BOS");
    fireEvent.click(screen.getByRole("link", { name: "closinglineup" }));

    expect(router.state.location.search).toBe("?date=2026-03-21&tab=flags&team=BOS");
    expect(await screen.findByRole("tab", { name: "Flags" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});

describe("/dev/charts", () => {
  it("renders every chart with sample data in dev, needing no API", async () => {
    const fetchMock = mockApi();
    renderApp("/dev/charts");

    expect(await screen.findByRole("heading", { name: "Charts" })).toBeInTheDocument();
    for (const chart of [
      "SparklineCell",
      "AnnotatedTimeSeries",
      "MoverChart",
      "StintTimeline",
      "ShotProfile",
    ]) {
      expect(screen.getByRole("heading", { name: chart })).toBeInTheDocument();
    }
    // Four sparklines and one of each other chart. The charts set their options in an effect, which
    // can land after the lazily loaded page's headings do.
    await waitFor(() => expect(allOptions()).toHaveLength(8));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

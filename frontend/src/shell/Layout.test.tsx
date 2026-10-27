import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  FRESH_META,
  LEAGUE_PAGE,
  mockApi,
  renderApp,
  SLATE_PAGE,
  TEAM_PAGE as BODY,
} from "../test/app";
import { easternToday } from "./params";

vi.mock("../components/charts/echarts", () => import("../test/echarts"));

const TEAM_PAGE = { body: BODY };
const FEED = { "/api/slate": { body: SLATE_PAGE }, "/api/league": { body: LEAGUE_PAGE } };

describe("Layout", () => {
  it("defaults the date picker to the latest derived date off the feed, reaching today", async () => {
    mockApi({ "/api/teams/BOS": TEAM_PAGE });
    renderApp("/teams/BOS");

    await waitFor(() => expect(screen.getByLabelText("Date")).toHaveValue("2026-03-20"));
    expect(screen.getByLabelText("Date")).toHaveAttribute("max", easternToday());
  });

  it("defaults the date picker to today on the feed, its morning", async () => {
    mockApi(FEED);
    renderApp("/");

    expect(screen.getByLabelText("Date")).toHaveValue(easternToday());
    expect(screen.getByLabelText("Date")).toHaveAttribute("max", easternToday());
    await screen.findByText(`Today: ${easternToday()}`);
  });

  it("puts a picked date in ?date= and refetches the page with it", async () => {
    const fetchMock = mockApi({ "/api/teams/BOS": TEAM_PAGE });
    const router = renderApp("/teams/BOS");
    await screen.findByText("Boston Celtics, as of 2026-03-19.");
    expect(fetchMock).toHaveBeenCalledWith("/api/teams/BOS?garbage_time=exclude");

    fireEvent.change(screen.getByLabelText("Date"), { target: { value: "2026-03-01" } });

    expect(router.state.location.search).toBe("?date=2026-03-01");
    expect(screen.getByLabelText("Date")).toHaveValue("2026-03-01");
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/teams/BOS?date=2026-03-01&garbage_time=exclude"),
    );
  });

  it("puts the garbage-time toggle in ?garbage_time= and refetches the page with it", async () => {
    const fetchMock = mockApi({ "/api/teams/BOS": TEAM_PAGE });
    const router = renderApp("/teams/BOS?date=2026-03-01");
    await screen.findByText("Boston Celtics, as of 2026-03-19.");

    fireEvent.click(screen.getByLabelText("Include garbage time"));

    expect(router.state.location.search).toBe("?date=2026-03-01&garbage_time=include");
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/teams/BOS?date=2026-03-01&garbage_time=include"),
    );
  });

  it("switches team, keeping the date", async () => {
    mockApi({ "/api/teams/BOS": TEAM_PAGE, "/api/teams/LAL": TEAM_PAGE });
    const router = renderApp("/teams/BOS?date=2026-03-01");
    const switcher = await screen.findByRole("combobox", { name: "Team" });
    await waitFor(() => expect(switcher).toHaveValue("BOS"));

    fireEvent.change(switcher, { target: { value: "LAL" } });

    expect(router.state.location.pathname).toBe("/teams/LAL");
    expect(router.state.location.search).toBe("?date=2026-03-01");
  });

  it("shows no stale banner when every source is fresh", async () => {
    mockApi(FEED);
    renderApp("/?date=2026-03-21");
    await screen.findByText("Last night: 2026-03-20");

    expect(screen.queryByRole("complementary", { name: "Stale data" })).not.toBeInTheDocument();
  });

  it("shows the stale banner when a team's injuries are ESPN's", async () => {
    const meta = {
      ...FRESH_META,
      injury_sources: [
        { tricode: "BOS", source: "pdf", stale: false },
        { tricode: "LAL", source: "espn", stale: true },
      ],
    };
    mockApi({ "/api/meta": { body: meta }, "/api/slate": "pending" });
    renderApp("/");

    expect(await screen.findByRole("complementary", { name: "Stale data" })).toHaveTextContent(
      "Injuries for LAL are from ESPN's fallback, not the official report.",
    );
  });

  it("shows the stale banner when a required source has given nothing", async () => {
    const meta = {
      ...FRESH_META,
      freshness: [...FRESH_META.freshness, { source: "bpm_local", latest: null }],
    };
    mockApi({ "/api/meta": { body: meta }, "/api/slate": "pending" });
    renderApp("/");

    expect(await screen.findByRole("complementary", { name: "Stale data" })).toHaveTextContent(
      "No data yet from: local BPM.",
    );
  });

  it("shows no stale banner for a source that may be switched off", async () => {
    const meta = {
      ...FRESH_META,
      freshness: [...FRESH_META.freshness, { source: "darko", latest: null }],
    };
    mockApi({ "/api/meta": { body: meta }, ...FEED });
    renderApp("/?date=2026-03-21");
    await screen.findByText("Last night: 2026-03-20");

    expect(screen.queryByRole("complementary", { name: "Stale data" })).not.toBeInTheDocument();
  });
});

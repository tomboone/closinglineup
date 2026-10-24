import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FRESH_META, mockApi, renderApp } from "../test/app";

const TEAM_PAGE = { body: { name: "Boston Celtics", date: "2026-03-20", tier_date: null } };

describe("Layout", () => {
  it("defaults the date picker to the latest derived date", async () => {
    mockApi({ "/api/teams/BOS": TEAM_PAGE });
    renderApp("/teams/BOS");

    await waitFor(() => expect(screen.getByLabelText("Date")).toHaveValue("2026-03-20"));
  });

  it("puts a picked date in ?date= and refetches the page with it", async () => {
    const fetchMock = mockApi({ "/api/teams/BOS": TEAM_PAGE });
    const router = renderApp("/teams/BOS");
    await screen.findByText(/Boston Celtics/);
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
    await screen.findByText(/Boston Celtics/);

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
    mockApi({ "/api/league": { body: { date: "2026-03-20" } } });
    renderApp("/");
    await screen.findByText("Morning feed for 2026-03-20.");

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
    mockApi({ "/api/meta": { body: meta }, "/api/league": "pending" });
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
    mockApi({ "/api/meta": { body: meta }, "/api/league": "pending" });
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
    mockApi({ "/api/meta": { body: meta }, "/api/league": { body: { date: "2026-03-20" } } });
    renderApp("/");
    await screen.findByText("Morning feed for 2026-03-20.");

    expect(screen.queryByRole("complementary", { name: "Stale data" })).not.toBeInTheDocument();
  });
});

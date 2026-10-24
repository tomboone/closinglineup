import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { vi } from "vitest";
import type { MetaResponse, PlayerPage, TeamPage, TeamsResponse } from "../api/client";
import { routes } from "../routes";

/** A mocked API reply: a JSON body with a status (200 if omitted), or a request that never ends. */
export type Reply = { status?: number; body: unknown } | "pending";

export const FRESH_META: MetaResponse = {
  latest_derived_date: "2026-03-20",
  jobs: [],
  freshness: [
    { source: "box_score", latest: "2026-03-20" },
    { source: "injury_pdf", latest: "2026-03-20T18:30:00-04:00" },
    { source: "injury_espn", latest: null },
    { source: "transactions", latest: "2026-03-20" },
  ],
  injury_sources: [{ tricode: "BOS", source: "pdf", stale: false }],
};

export const TEAMS: TeamsResponse = {
  season: "2025-26",
  teams: [
    {
      tricode: "BOS",
      name: "Boston Celtics",
      conference: "East",
      division: "Atlantic",
      wins: 50,
      losses: 20,
      latest_derived_date: "2026-03-20",
    },
    {
      tricode: "LAL",
      name: "Los Angeles Lakers",
      conference: "West",
      division: "Pacific",
      wins: 40,
      losses: 30,
      latest_derived_date: "2026-03-19",
    },
  ],
};

const series = (values: (number | null)[]) =>
  values.map((value, i) => ({ game_date: `2026-03-${String(10 + i).padStart(2, "0")}`, value }));

const player = (player_id: number, name: string) => ({ player_id, name });

const TATUM = player(1628369, "Jayson Tatum");
const BROWN = player(1627759, "Jaylen Brown");
const WHITE = player(1628401, "Derrick White");
const HOLIDAY = player(201950, "Jrue Holiday");
const PORZINGIS = player(204001, "Kristaps Porzingis");
const PRITCHARD = player(1630202, "Payton Pritchard");
const DAVISON = player(1630486, "JD Davison");

const boardRow = (
  ref: { player_id: number; name: string },
  tier: TeamPage["tier_board"][number]["tier"],
  extra: Partial<TeamPage["tier_board"][number]> = {},
): TeamPage["tier_board"][number] => ({
  player: ref,
  tier,
  badges: [],
  availability: "active",
  is_short: false,
  minutes_share_10: 0.1,
  starter_rate_5: 0,
  played_rate_10: 1,
  minutes_share: series([0.1, 0.11, 0.12]),
  on_off_net: series([2, -1, 4]),
  impact: series([1.1, 1.2, 1.3]),
  impact_source: "epm",
  ...extra,
});

/** A Celtics team page: one player in every tier but Fringe, out of order as the API never sends it. */
export const TEAM_PAGE: TeamPage = {
  tricode: "BOS",
  name: "Boston Celtics",
  date: "2026-03-20",
  tier_date: "2026-03-19",
  garbage_time: "exclude",
  tier_board: [
    boardRow(DAVISON, "out", { badges: ["two_way", "g_league"], availability: "g_league" }),
    boardRow(TATUM, "starter", { minutes_share_10: 0.15, starter_rate_5: 1 }),
    boardRow(PRITCHARD, "core", { is_short: true, impact_source: "darko" }),
    boardRow(BROWN, "starter", { minutes_share_10: 0.14, starter_rate_5: 1 }),
    boardRow(PORZINGIS, "injured", { availability: "out" }),
  ],
  closing_lineups: [
    {
      game_id: "0022500990",
      game_date: "2026-03-19",
      opponent: "NYK",
      margin: 4,
      players: [TATUM, BROWN, WHITE, HOLIDAY, PRITCHARD],
    },
  ],
  lineups: [
    {
      players: [TATUM, BROWN, WHITE, HOLIDAY, PRITCHARD],
      possessions: 812,
      opp_possessions: 808,
      net_rating: 9.4,
      possessions_10: 160,
      net_rating_10: 12.1,
    },
  ],
  combos: [
    {
      players: [TATUM, BROWN],
      possessions: 2410,
      opp_possessions: 2398,
      net_rating: 7.2,
      possessions_10: 410,
      net_rating_10: 3.5,
    },
  ],
  availability: {
    report_datetime: "2026-03-19T17:30:00-04:00",
    game_date: "2026-03-19",
    source: "pdf",
    stale: false,
    players: [
      { player: PORZINGIS, status: "Out", reason: "Injury/Illness - Left Ankle; Sprain", g_league: false },
      { player: DAVISON, status: null, reason: null, g_league: true },
    ],
  },
  roster: { standard: 14, standard_limit: 15, two_way: 3, two_way_limit: 3 },
  contracts: [
    {
      player: PRITCHARD,
      salary: 2_000_000,
      minimum_salary: true,
      guaranteed: 500_000,
      guarantee_date: "2026-04-10",
      guaranteed_after: 2_000_000,
      days_until_guarantee: 21,
    },
  ],
};

const shotZone = (
  zone: PlayerPage["shot_profile"][number]["zone"],
  freq: number,
): PlayerPage["shot_profile"][number] => ({
  zone,
  freq,
  fg_pct: 0.5,
  season_freq: freq + 0.02,
  season_fg_pct: 0.48,
  prior_freq: null,
  prior_fg_pct: null,
});

/**
 * Jaylen Brown's page over his last 10 games, three of them here, with an injury and a transaction
 * annotation on two of their dates.
 */
export const PLAYER_PAGE: PlayerPage = {
  date: "2026-03-20",
  window: "10",
  garbage_time: "exclude",
  header: { player: BROWN, tricode: "BOS", position: "G-F", tier: "starter", badges: [] },
  series: {
    minutes: series([34.5, 12, 36]),
    minutes_share: series([0.144, 0.05, 0.15]),
    on_court_net: series([8.2, -3, 11]),
    on_off_net: series([4.1, -6, 7.5]),
    ts_pct: series([0.61, null, 0.58]),
    usg_pct: series([0.29, null, 0.31]),
    impact: [
      { source: "epm", points: series([2.1, 2.0, 2.2]) },
      { source: "darko", points: series([1.4, 1.4, 1.5]) },
    ],
  },
  annotations: [
    { game_date: "2026-03-11", kind: "injury", label: "Available → Questionable (knee)" },
    { game_date: "2026-03-12", kind: "transaction", label: "Signed extension" },
  ],
  shot_profile: [shotZone("rim", 0.3), shotZone("corner_3", 0.1)],
  teammates: [
    { player: TATUM, possessions: 412, net_rating: 9.1 },
    { player: WHITE, possessions: 380, net_rating: -2.4 },
  ],
  matchups: {
    defenders: [
      {
        player: player(1628384, "OG Anunoby"),
        partial_poss: 41.5,
        pts: 12,
        fgm: 5,
        fga: 11,
        fg3m: 1,
        fg3a: 4,
        ast: 2,
        tov: 1,
      },
    ],
    guarded: [],
  },
  flags: [
    {
      game_date: "2026-03-12",
      flag: "minutes_cliff",
      player: BROWN,
      tricode: "BOS",
      detail: { share_5: 0.08, share_20: 0.14, ratio: 0.57 },
    },
  ],
};

/**
 * Stubs `fetch` with replies by path, `/api/meta` and `/api/teams` answering fresh by default; a
 * path with no reply is a 404. Returns the mock, whose calls are the URLs fetched.
 */
export function mockApi(replies: Record<string, Reply> = {}) {
  const all: Record<string, Reply> = {
    "/api/meta": { body: FRESH_META },
    "/api/teams": { body: TEAMS },
    ...replies,
  };
  const fetchMock = vi.fn((input: string) => {
    const reply = all[new URL(input, "http://localhost").pathname];
    if (reply === "pending") return new Promise<Response>(() => {});
    const { status = 200, body } = reply ?? { status: 404, body: { detail: "Not Found" } };
    return Promise.resolve(Response.json(body, { status }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Renders the app at `path` with a query client of its own; returns the router. */
export function renderApp(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

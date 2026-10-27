import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { vi } from "vitest";
import type {
  GamePage,
  LeaguePage,
  MetaResponse,
  PlayerPage,
  SlatePage,
  TeamPage,
  TeamsResponse,
} from "../api/client";
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
  ws: null,
  vorp: null,
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
    boardRow(TATUM, "starter", { minutes_share_10: 0.15, starter_rate_5: 1, ws: 4.25, vorp: 1.5 }),
    boardRow(PRITCHARD, "core", { is_short: true, impact_source: "darko", ws: 1.04, vorp: -0.36 }),
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

type ProductionRow = PlayerPage["production"][number];
type ProductionCell = NonNullable<ProductionRow["windows"]["season"]>;

const cell = (value: number, games: number, delta: number | null, isShort = false): ProductionCell => ({
  value,
  delta_season: delta,
  games,
  is_short: isShort,
  o_value: null,
  d_value: null,
});

/** A metric over 12 games of the season: the 20-game window short, the prior season whole. */
const production = (
  metric: ProductionRow["metric"],
  label: string,
  format: ProductionRow["format"],
  value: number,
): ProductionRow => ({
  metric,
  label,
  format,
  windows: {
    "5": cell(value * 1.1, 5, value * 0.1),
    "10": cell(value, 10, 0),
    "20": cell(value, 12, 0, true),
    season: cell(value, 12, 0),
    prior_season: cell(value * 0.9, 70, -value * 0.1),
  },
});

const PRODUCTION: ProductionRow[] = [
  production("ts_pct", "TS%", "percent", 0.6),
  production("efg_pct", "eFG%", "percent", 0.55),
  production("usg_pct", "USG%", "percent", 0.28),
  production("ast_pct", "AST%", "percent", 0.18),
  production("tov_pct", "TOV%", "percent", 0.11),
  production("oreb_pct", "OREB%", "percent", 0.04),
  production("dreb_pct", "DREB%", "percent", 0.14),
  production("ftr", "FTr", "rate", 0.3),
  production("fg3a_rate", "3PAr", "rate", 0.4),
  production("pts_per36", "PTS/36", "number", 25),
  production("reb_per36", "REB/36", "number", 6),
  production("ast_per36", "AST/36", "number", 4),
  production("game_score", "Game Score", "per_game", 16),
  {
    metric: "ws",
    label: "Win Shares",
    format: "number",
    windows: {
      "5": { ...cell(0.9, 5, null), o_value: 0.6, d_value: 0.3 },
      "10": { ...cell(1.8, 10, null), o_value: 1.2, d_value: 0.6 },
      "20": { ...cell(2.4, 12, null, true), o_value: 1.6, d_value: 0.8 },
      season: { ...cell(2.4, 12, null), o_value: 1.6, d_value: 0.8 },
      prior_season: { ...cell(7.1, 70, null), o_value: 4.5, d_value: 2.6 },
    },
  },
  production("ws_48", "WS/48", "rate", 0.15),
  {
    metric: "vorp",
    label: "VORP",
    format: "number",
    windows: { "5": null, "10": null, "20": null, season: cell(0.9, 12, null), prior_season: cell(2.8, 70, null) },
  },
];

/**
 * Jaylen Brown's page over his last 10 games, three of them here, with an injury and a transaction
 * annotation on two of their dates.
 */
export const PLAYER_PAGE: PlayerPage = {
  date: "2026-03-20",
  window: "10",
  garbage_time: "exclude",
  header: {
    player: BROWN,
    tricode: "BOS",
    position: "G-F",
    tier: "starter",
    availability: "questionable",
    badges: [],
    injury: { status: "Questionable", reason: "Left knee soreness" },
  },
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
  tiers: [
    { game_date: "2026-03-10", tier: "core" },
    { game_date: "2026-03-11", tier: null },
    { game_date: "2026-03-12", tier: "starter" },
  ],
  annotations: [
    { game_date: "2026-03-11", kind: "injury", label: "Available → Questionable (knee)" },
    { game_date: "2026-03-12", kind: "transaction", label: "Signed extension" },
    { game_date: "2026-03-12", kind: "tier", label: "Core → Starter" },
  ],
  shot_profile: [shotZone("rim", 0.3), shotZone("corner_3", 0.1)],
  production: PRODUCTION,
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
      impact: null,
      tiers: null,
    },
  ],
  contract: null,
};

const mover = (
  ref: { player_id: number; name: string },
  tricode: string,
  value: number,
  delta: number,
  source: LeaguePage["impact_risers"][number]["source"] = null,
): LeaguePage["minutes_risers"][number] => ({
  player: ref,
  tricode,
  value,
  delta,
  source,
  impact: null,
});

const signal = (name: string, label: string, value: number | string) => ({ name, label, value });

/**
 * The morning of 2026-03-20: two teams' tier transitions, a mover each way of both kinds, a flag of
 * two kinds, an injury change and a transaction.
 */
export const LEAGUE_PAGE: LeaguePage = {
  date: "2026-03-20",
  rotation_events: [
    {
      tricode: "BOS",
      events: [
        {
          player: PRITCHARD,
          game_date: "2026-03-20",
          from_tier: "fringe",
          to_tier: "core",
          signals: [
            signal("minutes_share", "minutes share up 4.1 pts vs 20-game", 0.041),
            signal("injury", "back from Out 2 days ago", "Out → Available"),
            signal("starter_rate", "starter rate up 20.0 pts vs 20-game", 0.2),
          ],
          impact: null,
        },
        {
          player: PORZINGIS,
          game_date: "2026-03-20",
          from_tier: "starter",
          to_tier: "injured",
          signals: [
            signal("injury", "listed Out today", "Available → Out"),
            signal("minutes_volatility", "minutes volatility up 6.2 min vs 20-game", 6.2),
            signal("usage", "usage down 2.0 pts vs prior 10 games", -0.02),
          ],
          impact: null,
        },
      ],
    },
    {
      tricode: "LAL",
      events: [
        {
          player: player(1642000, "Dalton Knecht"),
          game_date: "2026-03-20",
          from_tier: null,
          to_tier: "fringe",
          signals: [
            signal("transaction", "signed today", "signing"),
            signal("minutes_share", "minutes share up 2.0 pts vs 20-game", 0.02),
            signal("on_court_net", "on-court net down 3.1 vs prior 10 games", -3.1),
          ],
          impact: null,
        },
      ],
    },
  ],
  quiet_signals: [],
  minutes_risers: [mover(PRITCHARD, "BOS", 0.12, 0.041)],
  minutes_fallers: [mover(HOLIDAY, "BOS", 0.08, -0.05)],
  flags: [
    {
      game_date: "2026-03-20",
      flag: "minutes_cliff",
      player: HOLIDAY,
      tricode: "BOS",
      detail: { share_5: 0.05, share_20: 0.12, ratio: 0.42 },
      impact: null,
      tiers: null,
    },
    {
      game_date: "2026-03-20",
      flag: "contract_pressure",
      player: DAVISON,
      tricode: "BOS",
      detail: { reason: "two_way", active_games: 46, limit: 50 },
      impact: null,
      tiers: null,
    },
  ],
  injury_changes: [
    {
      player: PORZINGIS,
      tricode: "BOS",
      from_status: null,
      to_status: "Out",
      reason: "Injury/Illness - Left Ankle; Sprain",
      report_datetime: "2026-03-20T17:30:00-04:00",
    },
  ],
  transactions: [
    {
      date: "2026-03-20",
      type: "sign",
      detail: "Los Angeles Lakers signed Dalton Knecht to a 10-day contract.",
      player: player(1642000, "Dalton Knecht"),
      from_tricode: null,
      to_tricode: "LAL",
      impact: null,
    },
  ],
  impact_risers: [mover(TATUM, "BOS", 6.1, 0.8, "epm")],
  impact_fallers: [mover(BROWN, "BOS", 2.0, -0.6, "darko")],
};

/** A date no team played: every list empty (spec §8). */
export const EMPTY_LEAGUE_PAGE: LeaguePage = {
  date: "2026-03-21",
  rotation_events: [],
  quiet_signals: [],
  minutes_risers: [],
  minutes_fallers: [],
  flags: [],
  injury_changes: [],
  transactions: [],
  impact_risers: [],
  impact_fallers: [],
};

const slateTeam = (tricode: string, b2b = false): SlatePage["games"][number]["home"] => ({
  tricode,
  b2b,
  report: { report_datetime: null, source: null, stale: false, submitted: false },
  lines: [],
  projected_starters: [],
});

/** The morning of 2026-03-21, the day after `LEAGUE_PAGE`'s date: one game, no reports yet. */
export const SLATE_PAGE: SlatePage = {
  date: "2026-03-21",
  last_night: "2026-03-20",
  games: [
    {
      game_id: "0022501000",
      tip_time: "2026-03-21T19:30:00-04:00",
      away: slateTeam("LAL", true),
      home: slateTeam("BOS"),
    },
  ],
};

const KNICKS = {
  BRUNSON: player(1628973, "Jalen Brunson"),
  HART: player(1628404, "Josh Hart"),
  ANUNOBY: player(1628384, "OG Anunoby"),
  BRIDGES: player(1628969, "Mikal Bridges"),
  TOWNS: player(1626157, "Karl-Anthony Towns"),
  MCBRIDE: player(1630540, "Miles McBride"),
};

const gamePlayer = (
  ref: { player_id: number; name: string },
  is_starter: boolean,
  seconds: number,
) => ({ player: ref, is_starter, seconds });

const stint = (
  player_id: number,
  team_id: number,
  period: number,
  start_sec: number,
  end_sec: number,
  is_garbage_time = false,
): GamePage["stints"][number] => ({
  player_id,
  team_id,
  period,
  start_sec,
  end_sec,
  is_garbage_time,
});

const BOS_ID = 1610612738;
const NYK_ID = 1610612752;

/**
 * A Celtics blowout of the Knicks, so no closing lineup, with garbage time from 5:00 left in the
 * fourth: six players a side, box-score order, and a stint or two each.
 */
export const GAME_PAGE: GamePage = {
  game_id: "0022500990",
  game_date: "2026-03-19",
  status: "final",
  garbage_time: "exclude",
  away: {
    team_id: NYK_ID,
    tricode: "NYK",
    score: 101,
    players: [
      gamePlayer(KNICKS.BRUNSON, true, 2052),
      gamePlayer(KNICKS.HART, true, 1980),
      gamePlayer(KNICKS.ANUNOBY, true, 1905),
      gamePlayer(KNICKS.BRIDGES, true, 1860),
      gamePlayer(KNICKS.TOWNS, true, 1800),
      gamePlayer(KNICKS.MCBRIDE, false, 905),
    ],
    lineups: [
      {
        players: [KNICKS.BRUNSON, KNICKS.HART, KNICKS.ANUNOBY, KNICKS.BRIDGES, KNICKS.TOWNS],
        possessions: 48,
        opp_possessions: 47,
        pts_for: 50,
        pts_against: 58,
        net_rating: -19.2,
      },
    ],
    closing: null,
  },
  home: {
    team_id: BOS_ID,
    tricode: "BOS",
    score: 126,
    players: [
      gamePlayer(TATUM, true, 2010),
      gamePlayer(BROWN, true, 1950),
      gamePlayer(WHITE, true, 1890),
      gamePlayer(HOLIDAY, true, 1700),
      gamePlayer(PORZINGIS, true, 1600),
      gamePlayer(DAVISON, false, 300),
    ],
    lineups: [
      {
        players: [TATUM, BROWN, WHITE, HOLIDAY, PORZINGIS],
        possessions: 47,
        opp_possessions: 48,
        pts_for: 58,
        pts_against: 50,
        net_rating: 19.2,
      },
    ],
    closing: null,
  },
  stints: [
    stint(KNICKS.BRUNSON.player_id, NYK_ID, 1, 0, 480),
    stint(KNICKS.MCBRIDE.player_id, NYK_ID, 4, 420, 720, true),
    stint(TATUM.player_id, BOS_ID, 1, 0, 540),
    stint(TATUM.player_id, BOS_ID, 3, 0, 720),
    stint(DAVISON.player_id, BOS_ID, 4, 420, 720, true),
  ],
  garbage_time_from: { period: 4, start_sec: 420 },
};

/** The same game won by 4 instead, so both closing fives and no garbage time. */
export const CLOSE_GAME_PAGE: GamePage = {
  ...GAME_PAGE,
  away: {
    ...GAME_PAGE.away,
    score: 108,
    closing: [KNICKS.BRUNSON, KNICKS.HART, KNICKS.ANUNOBY, KNICKS.BRIDGES, KNICKS.MCBRIDE],
  },
  home: {
    ...GAME_PAGE.home,
    score: 112,
    closing: [TATUM, BROWN, WHITE, HOLIDAY, PORZINGIS],
  },
  stints: GAME_PAGE.stints.map((s) => ({ ...s, is_garbage_time: false })),
  garbage_time_from: null,
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

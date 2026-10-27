# NEU-2006: League table page

Ticket: NEU-2006 (story NEU-2002, milestone M5 Player-centred views). Blocked by NEU-2003, whose spec (`NEU-2003-windowed-win-shares.md`) defines the `ws_window` and `ws_48` columns this reads. Project spec: `closinglineup-v2-morning-feed-and-home-grown-metrics-project-spec.md` §15.4. Glossary: **League table** (`CONTEXT.md`).

## Why

There is no way to rank the league by the metrics the dashboard computes: finding the best WS/48, or which reserves carry the best impact, means opening player pages one by one. v2 ruled out a leaderboard on the feed. The user reversed that on 2026-10-27 for a separate page, and the feed stays about overnight change.

## Decisions

- **Every column covers the same games.** All of a player's games, whatever the team: the production rows. Minutes per game and games played exist today only on role rows, which are per team and restart when he joins a team, so they are added to the production rows. Minutes share is left out of the table: its denominator is his team's available minutes, which don't span a trade.
- **Small samples are filtered, not hidden in the sort.** Two minimums, both editable and in the URL:
  - minutes per game, default 15;
  - games played, defaulting to half the window's team games, rounded up (3 of 5, 5 of 10, 10 of 20, half the team games so far for Season and Prior season).
- **Prior season is a window.** Early in a season every window is a handful of games, so the selector is Last 5 / 10 / 20 / Season / Prior season. Prior season reads last season's `season` window at each player's last game of it, so everyone who played last season is listed, including anyone who hasn't played this one.
- **One sortable table, not leader lists**, and a page, not a feed tab (§15.4).

## What to build

### 1. The window view (`db/views.sql` and a migration recreating it)

Production rows (team_id NULL) gain `games_played` (games he had seconds in, the `played` sum `prod_window` already has) and `minutes_per_game` (`seconds / 60 / nullif(games_played, 0)`), in every window, the prior_season branch included. NEU-2003 carries `seconds` through `prod_metric`; carry `played` the same way. No deltas: the table doesn't show them, and role rows keep their own `minutes_per_game` with deltas for the player page.

If NEU-2003's view migration hasn't merged when this starts, rebase on it first. Both recreate the view, and this one's must contain NEU-2003's columns.

### 2. API: `GET /api/players` (in `api/routers/players.py`)

Query parameters: `date` (the shell's `DateDep`, resolving to the latest derived date on or before it) and `window: Literal["5", "10", "20", "season", "prior_season"] = "season"`.

The response, `LeagueTable {date, window, rows: LeagueTableRow[]}`, where each `LeagueTableRow` has these fields:

| Field | From |
|---|---|
| `player: PlayerRef` | names, one query |
| `tricode: str \| None`, `tier: Tier \| None` | his latest `rotation_state` on or before `date` in its season (the player header's rule); None when he has none |
| `games` | `games_in_window` (team games in the window) |
| `games_played`, `minutes_per_game` | the new production columns |
| `impact: Impact \| None` | the row's `darko`, else `bpm_local`, per `FEED_IMPACT_PREFERENCE` (no extra query) |
| `ws`, `ws_48` | `ws_window`, `ws_48` (NEU-2003) |
| `vorp` | `vorp_local` for Season and Prior season, None otherwise |
| `ts_pct`, `usg_pct`, `game_score` | the row's |

Rows: each player's latest production row in the window, read with one query. `DISTINCT ON (player_id) … WHERE team_id IS NULL AND "window" = :w AND game_date BETWEEN :start AND :date ORDER BY player_id, game_date DESC` serves the four current-season windows, with `start` the season's first day. The view has a row for a player only on his teams' game dates, so reading only `date` would drop every team idle that night. `prior_season` runs the same query with `"window" = 'season'` over the prior season's bounds. Unsorted and unfiltered: the page sorts and filters, so a filter change needs no refetch.

Schemas in `api/schemas.py`; OpenAPI snapshot and `frontend/src/api/types.ts` regenerated.

### 3. Frontend

- **Route and nav.** Route `/players`, `PlayersPage`. The shell header gets a "Players" link beside the team switcher, carrying the search string as the other links do.
- **Columns:** Player (link), Team, Tier, G (`games_played` of `games`), MPG, Impact (signed, source label), WS, WS/48, VORP, TS%, USG%, Game Score. Formats come from `components/format.ts`: `decimal`, `signed`, `ratio` for WS/48, `percent`.
- **Sorting.** Click a header to sort by it, descending; click again to flip. Nulls sort last either way. The default is WS/48, descending.
- **Controls:** the window selector, the minimum-MPG and minimum-games inputs, and the feed's `TeamFilter`.
- **URL state:**
  - `?window=`, shared with the player page, whose selector already falls back to its default for `prior_season`;
  - `?sort=<field>` and `&order=asc` when ascending;
  - `?min=` (MPG; absent means 15);
  - `?min_games=` (absent means half the window, per row);
  - `?team=`.
- **Count and empty state.** A line above the table reads "N players qualify". An empty result says which minimums to lower.

## Acceptance criteria

- `tests/test_windows.py`: a production row's `games_played` and `minutes_per_game` match the player's games and seconds across a trade (his games for both teams in one window).
- `tests/test_api_players.py`:
  - one row per seeded player, with `ws_48` and `ts_pct` as the view has them;
  - a player whose team was idle on `date` appears, with his team's last game's row;
  - `window=10` returns the 10-game values;
  - `window=prior_season` returns last season's season-window values, including a player with no game this season;
  - `vorp` is None for windows 5, 10 and 20.
- The route answers within two seconds over a full season's view (as `test_the_team_route_answers_within_two_seconds`).
- `PlayersPage.test.tsx`:
  - the default sort is WS/48;
  - clicking TS% sorts by it and sets `?sort=ts_pct`, and clicking again sets `&order=asc`;
  - `?min=30` hides a 20-minute player;
  - with no `?min_games=`, a player with 2 of 10 games is hidden in the 10-game window;
  - `?team=PHI` shows only PHI rows;
  - `?window=prior_season` fetches with `window=prior_season`.
- The shell header links to `/players`.
- Both CI gates pass.

## Out of scope

- A leaderboard on the feed.
- CSV export.
- Columns beyond those listed (per-36 counting stats, shot zones).
- Minutes share.
- Deltas.
- Server-side sorting or paging: a season is about 550 rows.

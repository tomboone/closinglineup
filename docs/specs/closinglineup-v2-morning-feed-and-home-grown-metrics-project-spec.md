# closinglineup v2: morning feed & home-grown metrics — project spec

Linear initiative: closinglineup (I-13). Project: P-NEU-103. Team: NEU. Glossary: `CONTEXT.md` at the repo root. Decisions and their rationale: `docs/adr/`. The v1 spec, `closinglineup-v1-rotation-and-value-dashboard-project-spec.md`, remains the reference for everything this document does not change: sources, data model, windows, tiers, events, the other five flags, deployment.

This spec is the shared reference for every ticket in the project. A ticket needs its own `/planit` spec only where this document is silent.

## 1. Purpose

v1's league page is a long scroll keyed to one game date, the latest derived, which is last night's. It opens on rotation changes, says "today" about last night, has no view of today's games, and its movers and flags are lists of numbers. v2 turns it into a **morning**: last night's derived changes and today's slate, in tabs, filterable by team, with the numbers behind movers and flags drawn rather than listed. It also gives the team page's sparklines a scale, and computes in-house the public-formula value metrics (Game Score, Win Shares, VORP) the loaded data already supports, surfacing them with the efficiency metrics the window view already holds.

Non-goals: mobile, auth, any new data source, PER, a leaderboard on the feed, flag-count history, any predictive model, changes to tiers or events. (A sortable league table on its own page was added in M5, §15.4.)

## 2. Decisions

| Area | Decision |
|---|---|
| Framing | The feed is a **morning**, a calendar date in Eastern time, with two dates: **last night**, the latest derived game date before it, and **today**, its slate. Every tab names which it reads (CONTEXT.md). |
| Injury cadence | A 07:30 ET `injuries` run joins the 10:00, 13:00, 16:00 and 18:30 runs, so the slate holds overnight changes. The 18:30 run stays the last, and the daily heartbeat's. |
| Tabs | Today · Rotation · Movers · Flags · Moves, opening on Today. The tab and the team filter live in the query string beside the date, so a morning is a URL. |
| Team filter | One multi-select of tricodes in the feed header, applied client-side to every tab. Movers rank league-wide and are then filtered, so a filtered player keeps his league rank. |
| Today | Per game: matchup, tip-off, which side is on a back-to-back, both teams' newest report lines for the game with each listed player's tier, 10-game minutes share and impact, changes since the previous report highlighted, and projected starters. Replaces v1's Injury changes section. |
| Quality on the feed | Every player row on the feed carries his current impact value, the first of DARKO and local BPM with a value, named by source. No leaderboard on the feed; the league table is its own page (M5, §15.4). |
| Movers | A diverging bar chart per metric with a labelled axis, the tables beneath. |
| Flags | Small visuals for minutes cliff, earning trust, bleeding and usage-up/efficiency-down; a 20-game tier strip for yo-yo; text for contract pressure. |
| Yo-yo rule | Counts only tier-to-tier events whose rotation state was not a short window. Arrivals and short-window events are excluded (§6). |
| Quiet signals | Starter and Core players who played under 60% of their 10-game average minutes last night or sat by coach's decision, without a rotation event, listed on the Rotation tab beneath the events. |
| Projected starters | A rule applied to the report, not a model: the five highest starter rates over the last five games, Out and Doubtful removed, gaps filled by the next (§5.3). |
| Metrics | Game Score, VORP and Win Shares computed in-house from public formulas, validated against published 2025-26 values as local BPM was (§8). eFG%, USG%, AST%, TOV%, OREB%, DREB%, FTr and 3PAr, already in the window view, are surfaced. |
| Metrics storage | Game Score is a window-view column. Win Shares and VORP are nightly `impact_snapshot` rows, sources `ws_local` and `vorp_local`, so the season-to-date and prior-season values ride the view's existing snapshot columns (§8.4). |
| Metrics on pages | Player page: a Production table, a row per metric, columns Last 5 / 10 / 20 / Season / Prior season, each cell with its delta vs season. Team tier board: season Win Shares and VORP columns. |
| Sparkline scale | Every row in a tier-board column shares one y range, printed in the column header; each line ends with its latest value as a label; hover shows date and value; the dashed zero baseline stays. |
| Empty states | An empty movers or flags list states the window it needs and how many games the filtered teams have, so a quiet morning reads as quiet rather than broken. |

## 3. The morning and its dates

- **Morning** `M`: the date the feed is opened on, the global date picker's value. It defaults to today in `TZ`; the picker's `max` becomes today, not the latest derived date. Pages other than the feed keep reading the picker as a game date, as in v1.
- **Last night** `L`: the latest derived game date strictly before `M` (`derive.run` counts one derived, v1 §8). If `nightly` has not run for `M − 1` yet, `L` is older, and the derived tabs say so: "Last night: Oct 24 (Oct 25 not derived yet)". A morning before the season's first derived date has no last night, and the derived tabs are empty with that message.
- **Today**: `M`'s slate, read from the schedule and the injury reports alone. Nothing on it is derived. A morning with no games has an empty slate: "No games on Oct 26."
- The Rotation, Movers, Flags and Moves tabs read `L` through v1's `GET /api/league?date=L`. Moves are `L`'s transactions and `M`'s so far, the date on each.
- The Today tab reads `GET /api/slate?date=M` (§9).

Picking a past morning works the same way: last night is the latest derived date before it, and the slate is as it stood by the end of that date (§5.1), so an old morning shows the final pregame reports.

## 4. Tabs

Route `/` keeps serving the feed; `?tab=today|rotation|movers|flags|moves` (M5 adds `last_night`, §15.3) picks the tab (`today` when absent), `?team=PHI,LAL` the filter (none when absent), beside v1's `?date=`. Tabs are `role="tablist"` buttons over one panel; switching tabs does not refetch. The feed header holds the morning's two dates, the tab list and the team filter. The `league-page` layout's one-column scroll goes.

### 4.1 Today

The slate, §5. Filtered to games either of whose teams is selected.

### 4.2 Rotation

v1's rotation events, grouped by team as now, each row with the player's impact value (§7). Beneath them, **Quiet signals** (§5.4). Filtered to the selected teams. Each event's "today" wording in its signal labels becomes the game date: `derive.events._ago` says "on Oct 25" for 0 days, "1 day before" and "n days before" for the rest, since the label is read a day or more later.

### 4.3 Movers

Two blocks, Minutes and Impact. Each is a diverging horizontal bar chart (`MoverChart`, §10) over the API's 15 (minutes) or 10 (impact) risers and fallers: risers above a zero line in the rising colour, fallers below in the falling colour, bar length the change (share in percentage points; impact in rating units, the source named in the block's note), the player's name and tricode on each bar, the axis labelled with units, hover giving the prior and latest values, click going to the player. The v1 tables follow each chart for the exact numbers, with the impact column of §7 added. Filtered to the selected teams after ranking.

### 4.4 Flags

v1's groups by flag, each row with the text line, the impact value, and the flag's visual (§6). Filtered to the selected teams.

### 4.5 Moves

v1's transactions list, for `L` and `M`, with the impact value.

## 5. The slate

### 5.1 Reports

For each game on `M`, each team's report is its newest `injury_status` rows for that game (`game_id`, or for ESPN rows, `team_id` and `game_date = M`) reported by the end of `M` Eastern, the official source on a tie, as the team page's availability board reads one (v1 §4.3, `derive.availability`). A G League line is an assignment, not an injury, and shows as the `g_league` badge, not a line (v1 §7.5). Its `previous` report is the team's newest for the same game reported by the end of `M − 1`; absent, every line is new.

### 5.2 Lines

Each line: player, status, reason, his tier and `minutes_share_10` from his `rotation_state` on the team's latest derived date on or before `L` (null before his first), his impact (§7), and a `change` against the previous report:

| `change` | When |
|---|---|
| `new` | not on the previous report, or on it but not Out or Doubtful, and now Out or Doubtful |
| `upgraded` | on the previous report at a worse status (Out > Doubtful > Questionable > Probable > Available) and now a better one, Available included |
| `downgraded` | on the previous report at a better status and now a worse one, short of Out or Doubtful |
| `off` | on the previous report and not on this one; the line is shown with its previous status and no current one |
| `null` | unchanged |

Lines are ordered Out, Doubtful, Questionable, Probable, Available, then by tier, then name; `off` lines last. A team with no report by the end of `M` shows "Not yet submitted" and its roster's G League badges alone. The report's time, source and stale marker are shown per team as the availability board shows them.

### 5.3 Projected starters

For each team on the slate: its players ranked by `starter_rate_5` on the team's latest derived date, ties by `minutes_share_10`, then name; the first five whose line on the team's report for the game is not Out or Doubtful are the projected starters, each marked `replacement` when at least one player ranked above him was removed. A team with fewer than five players with a starter rate shows what it has. Not a prediction (CONTEXT.md).

### 5.4 Quiet signals

On `L`, each player whose `rotation_state` tier on `L` is Starter or Core, who has no `rotation_event` on `L`, and whose `player_game` line for his team's game on `L` has `dnp_reason = 'cd'`, or no `dnp_reason` and `seconds < 0.6 × minutes_per_game_10 × 60`, `minutes_per_game` from his 10-game role row on the team's previous game date (so last night's game is not in the average). A DNP for any other reason (injury, inactive, suspension, G League) is not a quiet signal, though its `seconds` is 0: he neither played nor sat by coach's decision, and an injury reaches the feed as an injury change. Each carries the minutes played, the 10-game average, the DNP reason and his impact. Ordered with a DNP-CD first, then by the shortfall, the largest first. Read at request time; not stored.

### 5.5 Tip-off and back-to-backs

`game` gains `tip_time` (`timestamptz`, nullable), from the schedule's `gameDateTimeEst` (stated in Eastern time, so parsed with `EASTERN`), filled by the schedule refresh and a one-off backfill. `home_b2b` and `away_b2b` are already on `game`. The slate shows tip-off in Eastern time and a "B2B" marker on the side playing its second night.

## 6. Flags

### 6.1 Yo-yo rule

`yo_yo` counts, in the team's last 20 final games this season, the date included, the player-team's rotation events with `from_tier` not NULL whose `rotation_state` row on the event's date has `is_short = false`. Arrivals and events raised while his role window was still filling do not count; a mid-season signing yo-yos once his window is full. `FlagRule.yo_yo_events` (3) and `yo_yo_games` (20) are unchanged. `tests/derive/cases/*.yml` gains cases for an early-season non-yo-yo and a mid-season signing; existing yo-yo cases are re-labelled where the rule changes them.

### 6.2 Visuals

Each raised flag's row shows a visual from its `detail`, drawn by `FlagVisual` (§10), ~160 × 40 px, with the same values as the text line:

| Flag | Visual |
|---|---|
| `minutes_cliff` | two horizontal bars, 5-game and 20-game share, one scale, the 60% line marked |
| `earning_trust` | a three-point step line over the three 5-game shares, the oldest first |
| `bleeding` | two bars around zero, on-court net and off-court net |
| `usage_up_efficiency_down` | two signed bars, USG delta and TS delta in points |
| `yo_yo` | a 20-cell strip, one per team game, the oldest first, each in its tier's colour (`TIER_COLOURS`), a gap for a date he had no row; hover gives the date and tier |
| `contract_pressure` | none |

The yo-yo strip's tiers are not in `detail`; `RaisedFlag` gains `tiers: [{game_date, tier}] | null`, filled for `yo_yo` from `rotation_state` over the team's last 20 final game dates through the flag's date.

## 7. Impact on the feed

Every player row on the feed (events, movers, flags, slate lines, projected starters, quiet signals, transactions) carries `impact: {source, value} | null`: the player's latest `impact_snapshot` on or before the row's date (`L` for derived rows, `M` for the slate) in the first of `IMPACT_PREFERENCE` (`darko`, then `bpm_local`; EPM is cancelled, NEU-1870) with a value; one query per response over the response's players. Shown as a signed number with its source label beside the name, as the team board shows it.

## 8. Home-grown metrics

### 8.1 Game Score

Hollinger's Game Score, `PTS + 0.4·FGM − 0.7·FGA − 0.4·(FTA − FTM) + 0.7·OREB + 0.3·DREB + STL + 0.7·AST + 0.7·BLK − 0.4·PF − TOV`, is linear in the box line, so a window's average Game Score is the formula over the window's sums divided by games played. `player_window_stats` gains `game_score` (per game played) on production rows, with `_delta_prior` and `_delta_season`, in `db/views.sql` and a migration recreating the view (`tests/test_windows.py` keeps the two the same). NULL with no games played.

### 8.2 Win Shares

Basketball Reference's published method ("Calculating Win Shares": Dean Oliver's individual points produced and offensive possessions for OWS; individual defensive rating from team stops for DWS; marginal points per win `0.32 × league PPG × team pace / league pace`), transcribed into `derive/value.py`'s docstring with its inputs named. No request is made to Basketball Reference; the formulas are public and the validation values are entered by hand (§8.5).

- Computed per player-season through the date, over regular-season games (`REGULAR_SEASON` phases, as flags count them), summing each game's components under that game's team context: a traded player's season is the sum over both teams, as the published TOT row is.
- Possessions are estimated with the published formula over box totals, not read from `stint`, so the result validates against the published values; `derive/value.py` says so.
- League context (PPG, pace, points per possession) is the season through the date.
- Written as `impact_snapshot(date, player_id, source='ws_local', value=WS, o_value=OWS, d_value=DWS, minutes_basis=minutes)`.

### 8.3 VORP

`VORP = (BPM + 2.0) × (5 × minutes share) × (team games / 82)`, BPM the player's `bpm_local` on the date, minutes share §7.1's over his production window's season (his minutes over his teams' available player-minutes in the games he was rostered for, so `5 ×` is his fraction of a team's game minutes), team games the games his teams played while he was rostered. Written as `impact_snapshot(date, player_id, source='vorp_local', value=VORP, minutes_basis=minutes)`, `o_value` and `d_value` NULL.

### 8.4 Storage and the view

`impact_snapshot.source` admits `ws_local` and `vorp_local` (migration). `derive/value.py` runs as a `nightly` step after `bpm`, recorded in `load_log` as (`impact_snapshot`, `value`), for the season's dates as `bpm` is, and once for the prior season's final date so the prior-season window has values. `player_window_stats`' production rows carry `ws_local`, `ws_local_o`, `ws_local_d` and `vorp_local` as they carry `bpm_local`, the prior_season row reading the prior season's final snapshot. `IMPACT_PREFERENCE` and `ImpactSeries` do not include them: they are value metrics, not impact ratings, and the player page's impact chart stays as it is.

### 8.5 Validation

`tests/test_value.py`, built like `tests/test_bpm.py` (NEU-1872): a hand-entered fixture of published 2025-26 end-of-season values for every player with 1,000+ minutes. Game Score: exact against a hand-computed box line. Win Shares: within ±0.5 WS of the published value for each such player, and the league's WS total within 3% of the league's wins. VORP: within ±0.3. The fixture's provenance (where and when it was copied) is in its header.

### 8.6 Production table

`GET /api/players/{id}` gains `production: ProductionRow[]`, a row per metric over the production windows at the date: `metric`, `label`, `format` (`percent` | `rate` | `number` | `per_game`), and `windows: {5, 10, 20, season, prior_season}` each `{value, delta_season, games, is_short} | null`. Metrics, in order: TS%, eFG%, USG%, AST%, TOV%, OREB%, DREB%, FTr, 3PAr, PTS/36, REB/36, AST/36, Game Score, Win Shares (season and prior only, with OWS/DWS in the hover), VORP (season and prior only). Rendered as a table under the player page's charts; a short window is marked as the tier board marks one.

### 8.7 Team board columns

`TierBoardRow` gains `ws: number | null` and `vorp: number | null`, the season window's values on the tier date; the board shows them as two number columns after the impact sparkline.

## 9. API

| Route | Change |
|---|---|
| `GET /api/league?date=` | Unchanged shape plus: `impact` on every player row; `quiet_signals: QuietSignal[]`; `RaisedFlag.tiers` (§6.2); `transactions` cover the date and the day after (§3). The route stays keyed to a game date: the frontend passes `L`. |
| `GET /api/slate?date=` | New. `{date, last_night: date \| null, games: SlateGame[]}`. `SlateGame`: `game_id`, `tip_time`, `home`, `away`; `SlateTeam`: `tricode`, `b2b`, `report: {report_datetime, source, stale, submitted} \| null`, `lines: SlateLine[]`, `projected_starters: ProjectedStarter[]`. A date with no games is an empty list, not a 404. |
| `GET /api/meta` | Unchanged; `latest_derived_date` is how the frontend finds `L` (the latest derived date on or before `M − 1`, so a new `derived_dates` is not needed: the feed asks `/api/league?date=` with `M − 1` and reads the response's `date`, which the route resolves to the latest derived date on or before it, as v1's `DateDep` does). |
| `GET /api/teams/{tricode}` | `TierBoardRow.ws`, `.vorp` (§8.7). |
| `GET /api/players/{id}` | `production` (§8.6). |

Schemas in `api/schemas.py`; the OpenAPI snapshot and `src/api/types.ts` regenerated. The slate route is served by `api/routers/slate.py`.

## 10. Frontend

- `LeaguePage` becomes the feed: header (dates, tabs, filter), one panel per tab, `useShellParams` extended with `tab` and `teams`. `TeamFilter` is a multi-select of the 30 tricodes from `useTeams`.
- New chart components, ECharts as before: `MoverChart` (diverging horizontal bars), `FlagVisual` (the five small drawings of §6.2, one component switching on the flag), `TierStrip` (the yo-yo strip; also usable elsewhere later).
- `SparklineCell` gains `range` as required for the tier board, an `endLabel` (the latest value through the column's formatter), and a tooltip; `TierBoard` computes each column's range over its rows, widened to the baseline, and prints it in the header: "Min share (0–32%)", "On/off net (−15 to +15)", "DARKO (−4 to +6)" — the impact header naming the source when every row shares one, "Impact" otherwise.
- Every chart with an axis labels it with units (the dataviz rules apply to `MoverChart`; the sparkline's scale is in the header by decision).
- The `dev/ChartsPage` gallery gets the new components with fixture data.
- Tests: a render test per new component; the feed page's smoke test covers each tab and the filter; `routes.test.tsx` covers `?tab=` and `?team=`.

## 11. Scheduling

`injuries_trigger` adds `CronTrigger(hour=7, minute=30)`. `injuries_now` already marks only the 18:30 run as the day's last, so the heartbeat is unchanged. `deploy/README.md`'s job table and the v1 spec's §3.3 row are updated. The schedule refresh in `nightly` and the backfill fill `tip_time`.

## 12. Testing

- Backend: `tests/test_slate.py` (route, against the seeded test database: changes of each kind, a not-submitted team, projected starters with a replacement, a no-games date), `tests/test_league.py` additions (quiet signals, impact, yo-yo tiers), `tests/test_flags.py` and the case suite for the yo-yo rule, `tests/test_value.py` (§8.5), `tests/test_windows.py` for `game_score`, `tests/test_scheduler.py` for the 07:30 trigger, the OpenAPI snapshot.
- Frontend: as §10.
- CI gate unchanged: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` in `backend/`; `pnpm lint && pnpm tsc -b && pnpm test` in `frontend/`.

## 13. Milestones

The Linear project (P-NEU-103) is the authoritative list. Stories NEU-1978..1981, one per milestone; work tickets NEU-1982..1999. NEU-1997 (Win Shares) is labelled `planit`: the spec defers its transcription and per-date league-context accumulation.

| Milestone | Story | Tickets |
|---|---|---|
| M1 Morning backend | NEU-1978 | NEU-1982 (07:30 injuries), NEU-1983 (tip_time), NEU-1984 (yo-yo rule), NEU-1985 (league: impact, tiers, wording, moves), NEU-1986 (quiet signals), NEU-1987 (slate route), NEU-1988 (projected starters) |
| M2 Tabbed feed | NEU-1979 | NEU-1989 (feed shell), NEU-1990 (Today tab), NEU-1991 (quiet signals, impact, empty states) |
| M3 Charts with scale | NEU-1980 | NEU-1992 (sparkline scale), NEU-1993 (MoverChart), NEU-1994 (FlagVisual, TierStrip) |
| M4 Home-grown metrics | NEU-1981 | NEU-1995 (Game Score), NEU-1996 (VORP, value step), NEU-1997 (Win Shares, `planit`), NEU-1998 (view columns, production table), NEU-1999 (tier board columns), NEU-2001 (team page tabs) |
| M5 Player-centred views | NEU-2002 | NEU-2003 (windowed WS, WS/48), NEU-2004 (player page parity), NEU-2005 (Last night tab), NEU-2006 (league table page) |

Order: M1 → M2 → M3; M4 is independent; M5 follows M4, and NEU-2006 follows NEU-2003. Blockers are in Linear.

## 14. Acceptance criteria for v2

- Opening `/` on a game-day morning shows today's slate with every game, each team's newest report (the 07:30 run's when it landed), lines with tier, share and impact, changes highlighted, and projected starters; the derived tabs name last night's date.
- `?tab=`, `?team=` and `?date=` reproduce a morning from its URL.
- Once 20 team games are in, the Movers tab draws both charts with labelled axes and the same players as the tables.
- Every raised flag but contract pressure shows its visual; yo-yo does not fire on arrivals or short windows in the case suite.
- Win Shares and VORP validate within §8.5's tolerances; Game Score matches a hand-computed line; the player page's production table shows every listed metric over five windows.
- Tier-board sparklines share a range per column, print it in the header, and label the latest value.
- Zero new data sources; no Basketball Reference requests.

## 15. M5: player-centred views

Added 2026-10-27, after NEU-1999 and NEU-2001: what the dashboard says about a player belongs on his page; Win Shares get windows; last night's games get a feed tab; the league gets a sortable table on its own page.

### 15.1 Windowed Win Shares (NEU-2003)

`player_window_stats`' production rows gain `ws_window`, `ws_window_o`, `ws_window_d`: in the 5, 10 and 20-game windows, the season-to-date snapshot at the date less the one at the date n team games earlier (0 before the season's first game; NULL when no snapshot exists at the window's start), the view's to-date-less-lag pattern; in the season window `ws_local`; in prior_season the prior season's final. `ws_48 = ws_window × 2880 / seconds` (NULL with no seconds), with `ws_48_delta_prior` and `_delta_season`. `derive` now writes BPM and the value snapshots before the view refresh (ADR 0005), so a window's end is the date's own snapshot. Detail: `NEU-2003-windowed-win-shares.md`. The production table's Win Shares row fills every window, `delta_season` null (a window's total and the season's do not compare), OWS/DWS in the hover; a WS/48 row (`rate`) follows it with deltas. VORP is unchanged: its trend is BPM's and minutes share's, which the page already charts.

### 15.2 Player page parity (NEU-2004)

`PlayerHeader` gains `availability` (his `rotation_state` on the latest state date, the row `tier` reads) and `injury: {status, reason} | null` (his line on his team's newest report by the end of the date). `PlayerPage` gains `tiers: TierPoint[]` (his tier per team game date in the window) and `contract: ContractNote | null` (the team page's rule: the latest weekly snapshot on or before the date, guarantee date still to come). `Annotation.kind` admits `tier`, one per `rotation_event` in the window ("Fringe → Core", "Joined as Core"). The page shows a `TierStrip` above the charts, availability and the injury line in the header, and the contract line under it. Mover rank and quiet signals stay on the feed: they are a morning's, with no history.

### 15.3 Last night tab (NEU-2005)

Tabs become Today · Last night · Rotation · Movers · Flags · Moves; `?tab=last_night`. `LeaguePage.games: LastNightGame[]`: L's final games, each `game_id`, `home` and `away` (`tricode`, `score`, `top`), `margin`; `top` is the side's two highest Game Scores, `{player, game_score, pts, reb, ast, seconds}`, Game Score §8.1's formula over the `player_game` line (`derive/value.py` `game_score()`, tested against the view's season window at a player's first game, the only one-game window it has). A row per game links to its game page. Filtered by `?team=` like the other tabs.

### 15.4 League table (NEU-2006)

`GET /api/players?date=&window=` (`window` as the player page's plus `prior_season`, default `season`) returns `LeagueTable {date, window, rows}`, a row per player from his latest production row on or before `date` in its season (the view has a row only on his team's game dates, so the latest derived date alone would drop idle teams): `player`, `tricode`, `tier`, `games`, `games_played` and `minutes_per_game` (added to production rows, so every column covers the same games; minutes share is left out, its per-team denominator not spanning a trade), `impact` (§7's), `ws`, `ws_48`, `vorp` (season and prior_season only), `ts_pct`, `usg_pct`, `game_score`. One query on the view; the team route's two-second budget. Page `/players`, linked from the shell header: one table, sortable by any column (descending first, WS/48 by default), with a window selector (Last 5 / 10 / 20 / Season / Prior season), a minimum minutes per game (default 15), a minimum games played (default half the window's games) and the feed's team filter, all in the URL (`?sort=`, `?window=`, `?min=`, `?min_games=`, `?team=`). Detail: `NEU-2006-league-table.md`. Not per-stat leader lists: they repeat the same players, and one sortable table answers each ranking. Not a feed tab: leaders move slowly, and the feed is about overnight change.

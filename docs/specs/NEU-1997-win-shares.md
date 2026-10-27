# NEU-1997: Win Shares in `derive/value.py` with the published-value validation suite

Linear: [NEU-1997](https://linear.app/neuroticsasquatch/issue/NEU-1997), milestone M4 "Home-grown metrics", story NEU-1981, blocked by NEU-1996 (Done, PR #86). Project spec: `closinglineup-v2-morning-feed-and-home-grown-metrics-project-spec.md` §8.2, §8.4, §8.5. Glossary: `CONTEXT.md` ("Value metric", "Impact snapshot").

## What and why

Win Shares is the one widely read value metric with a public method and no home in the dashboard, because its publisher forbids scraping. Every input is already in `player_game` and `game`. NEU-1996 shipped the `value` derive step, the `ws_local` snapshot source, the nightly wiring after `bpm`, the prior-season run path, and a `compute_value` that writes VORP and clears any `ws_local` rows for its date. This ticket makes that stub compute Win Shares.

Basketball Reference's published method ("Calculating Win Shares", with the individual offensive and defensive ratings from Dean Oliver's *Basketball on Paper*) is transcribed into `derive/value.py`, computed per player-team stint over season-to-date totals, summed into one row per player, and validated against the published 2025-26 end-of-season values from a hand-entered fixture. No request is made to Basketball Reference from the codebase.

## Decisions

| # | Decision | Rejected and why |
|---|---|---|
| 1 | **Unit of calculation: per player-team stint over season-to-date totals.** Each (player, team) stint is computed from the player's totals on that team and the team's totals through the date, exactly as the published per-team row is; the player's `ws_local` row is the sum of his stints, as the published `TOT` row is. | Per-game components summed: Oliver's formulas are nonlinear (assist adjustment, free-throw squares, team ORB weight), so the sum diverges from the published value and the ±0.5 check would measure the wrong thing. Whole-season totals with a blended team context: no published row to validate a traded player against. |
| 2 | **League and team context: one scan per date through `derive.bpm.season_lines`, widened.** `PlayerLine` gains `fgm` and `ftm`; `TeamLine` gains its own and its opponents' box totals (see Inputs). League context is the sum over the returned teams. Same cost profile as `bpm` today, which already rescans the season's `player_game` once per derived date; one loader keeps the two-sides-present rule and the score fallback in one place. | A second loader in `value.py`: duplicates the game-filtering rules, and the two drift. A per-season running-totals table: a migration, a maintenance step and a second source of truth to rebuild whenever a box is reloaded or reparsed, to save a scan nobody has measured as slow. |
| 3 | **Row set: every player with `bpm.MIN_MINUTES` (a minute) or more through the date**, so `ws_local`, `vorp_local` and `bpm_local` cover the same players. A DNP-only player has no row. | Every player with any box line: zero-minute players would need a special case in the stop rate (0/0) and get rows no other local source writes. |
| 4 | **Possessions: `derive.bpm.possessions`** (the two-sided Basketball Reference estimate over box totals), for team possessions, pace and the defensive rating. Individual offensive possessions are Oliver's `ScPoss + FGxPoss + FTxPoss + TOV`. | Possessions from `stint`: truer, but then nothing published validates the result (ticket). |
| 5 | **Validation: OWS, DWS and WS each within ±0.5 of the published value for every 1,000+ minute player-team row; the league's WS total within 3% of 1,230** (82 × 30 / 2). Checking both halves catches a transcription slip one half would otherwise hide in the sum. | WS only, as the project spec minimally says: a compensating error passes. ±0.2: fails on possession-estimate drift that is not a bug. |
| 6 | **Fixture provenance: the implementer reads the pages once with a throwaway parser outside the repo** and records page, date and join check in `tests/fixtures/bref/README.md`, as the BPM and VORP fixtures were. | The user copying values by hand: a manual step before the ticket can finish, for the same provenance. |

Not an ADR: the box-versus-`stint` choice is recorded in the project spec and the step is rewritten nightly, so it is cheap to reverse.

## The method (what the docstring must state)

Notation: a player's totals on one team through the date are `MP, FGM, FGA, FG3M, FTM, FTA, ORB, DRB, AST, STL, BLK, TOV, PF, PTS`; the team's through the date carry a `Tm_` prefix and its opponents' in those games an `Opp_` prefix. `Tm_MP` is the five players' minutes together (five times the game clock: `TeamLine.minutes × 5`). `Opp_MP` is taken equal to `Tm_MP`, since both are the same games' clocks. `Tm_Poss` is `derive.bpm.possessions(team box, opponent box)`. League totals (`Lg_`) are the sums over every team the loader returns: `Lg_PTS`, `Lg_Poss`, and `Lg_G`, the number of team-games (each game counted once per team).

Pace: `Tm_Pace = 48 × Tm_Poss / (Tm_MP / 5)`; `Lg_Pace = 48 × Lg_Poss / Σ(Tm_MP / 5)`.

**Marginal points per win** (the same for both halves): `0.32 × (Lg_PTS / Lg_G) × (Tm_Pace / Lg_Pace)`.

### Offensive Win Shares

Team terms:

- `Team_Scoring_Poss = Tm_FGM + (1 − (1 − Tm_FTM / Tm_FTA)²) × Tm_FTA × 0.4`
- `Team_ORB% = Tm_ORB / (Tm_ORB + Opp_DRB)`
- `Team_Play% = Team_Scoring_Poss / (Tm_FGA + Tm_FTA × 0.4 + Tm_TOV)`
- `Team_ORB_Weight = ((1 − Team_ORB%) × Team_Play%) / ((1 − Team_ORB%) × Team_Play% + Team_ORB% × (1 − Team_Play%))`

Player terms:

- `qAST = (MP / (Tm_MP / 5)) × 1.14 × ((Tm_AST − AST) / Tm_FGM) + ((Tm_AST / Tm_MP × MP × 5 − AST) / (Tm_FGM / Tm_MP × MP × 5 − FGM)) × (1 − MP / (Tm_MP / 5))`
- `FG_Part = FGM × (1 − 0.5 × ((PTS − FTM) / (2 × FGA)) × qAST)`
- `AST_Part = 0.5 × (((Tm_PTS − Tm_FTM) − (PTS − FTM)) / (2 × (Tm_FGA − FGA))) × AST`
- `FT_Part = (1 − (1 − FTM / FTA)²) × 0.4 × FTA`
- `ORB_Part = ORB × Team_ORB_Weight × Team_Play%`
- `ScPoss = (FG_Part + AST_Part + FT_Part) × (1 − (Tm_ORB / Team_Scoring_Poss) × Team_ORB_Weight × Team_Play%) + ORB_Part`
- `FGxPoss = (FGA − FGM) × (1 − 1.07 × Team_ORB%)`
- `FTxPoss = (1 − FTM / FTA)² × 0.4 × FTA`
- `TotPoss = ScPoss + FGxPoss + FTxPoss + TOV`

Points produced:

- `PProd_FG_Part = 2 × (FGM + 0.5 × FG3M) × (1 − 0.5 × ((PTS − FTM) / (2 × FGA)) × qAST)`
- `PProd_AST_Part = 2 × ((Tm_FGM − FGM + 0.5 × (Tm_FG3M − FG3M)) / (Tm_FGM − FGM)) × 0.5 × (((Tm_PTS − Tm_FTM) − (PTS − FTM)) / (2 × (Tm_FGA − FGA))) × AST`
- `PProd_ORB_Part = ORB × Team_ORB_Weight × Team_Play% × (Tm_PTS / (Tm_FGM + (1 − (1 − Tm_FTM / Tm_FTA)²) × 0.4 × Tm_FTA))`
- `PProd = (PProd_FG_Part + PProd_AST_Part + FTM) × (1 − (Tm_ORB / Team_Scoring_Poss) × Team_ORB_Weight × Team_Play%) + PProd_ORB_Part`

Then `Marginal_Offense = PProd − 0.92 × (Lg_PTS / Lg_Poss) × TotPoss` and `OWS = Marginal_Offense / Marginal_Points_Per_Win`.

### Defensive Win Shares

- `DOR% = Opp_ORB / (Opp_ORB + Tm_DRB)`
- `DFG% = Opp_FGM / Opp_FGA`
- `FMwt = (DFG% × (1 − DOR%)) / (DFG% × (1 − DOR%) + (1 − DFG%) × DOR%)`
- `Stops1 = STL + BLK × FMwt × (1 − 1.07 × DOR%) + DRB × (1 − FMwt)`
- `Stops2 = (((Opp_FGA − Opp_FGM − Tm_BLK) / Tm_MP) × FMwt × (1 − 1.07 × DOR%) + (Opp_TOV − Tm_STL) / Tm_MP) × MP + (PF / Tm_PF) × 0.4 × Opp_FTA × (1 − Opp_FTM / Opp_FTA)²`
- `Stop% = ((Stops1 + Stops2) × Opp_MP) / (Tm_Poss × MP)`
- `Team_DRtg = 100 × Opp_PTS / Tm_Poss`
- `D_Pts_per_ScPoss = Opp_PTS / (Opp_FGM + (1 − (1 − Opp_FTM / Opp_FTA)²) × Opp_FTA × 0.4)`
- `DRtg = Team_DRtg + 0.2 × (100 × D_Pts_per_ScPoss × (1 − Stop%) − Team_DRtg)`

Then `Marginal_Defense = (MP / Tm_MP) × Tm_Poss × (1.08 × (Lg_PTS / Lg_Poss) − DRtg / 100)` and `DWS = Marginal_Defense / Marginal_Points_Per_Win`.

`WS = OWS + DWS`.

### Zero guards

A ratio whose denominator is zero is zero: `FTA = 0` makes `FT_Part`, `FTxPoss` and the free-throw squares zero; `FGA = 0` makes the `(PTS − FTM) / (2 × FGA)` factor zero (so `FG_Part` and `PProd_FG_Part` are zero, `FGM` being zero too); `Tm_PF = 0` makes the foul share zero. A stint on a team whose `Tm_Poss`, `Team_Scoring_Poss`, `Tm_FGA`, `Tm_FTA`, `Opp_FGA` or `Opp_FTA` is zero (a team with no real box yet, as the seeded tests' filler teams) yields no stint, so no row, as `derive.bpm._team_bpm` returns nothing for a team without possessions.

### Where this departs from the published values (docstring)

- `Tm_TOV` and `Opp_TOV` are the players' sums; turnovers charged to the team are not in `player_game` (as `derive/bpm.py` already notes). They enter only `Team_Play%` and the possession estimate, and the league context moves the same way, so the effect on a season's WS is hundredths.
- `Tm_MP` is the players' seconds summed; the published figure is the same quantity rounded to minutes.
- A game with rows for only one side is left out of both teams' totals (the loader's existing rule).

## Inputs and the loader

`derive/bpm.py`:

- `PlayerLine` gains `fgm: int` and `ftm: int` (its `_Sums` already tracks `fgm`; add `ftm`).
- A new `SeasonBox` (or widening of `TeamBox`) carries `fgm, fga, fg3m, ftm, fta, orb, drb, ast, stl, blk, tov, pf, pts`; `TeamLine` gains `box: SeasonBox` (its own players' sums) and `opp_box: SeasonBox` (its opponents' players' sums in the same games). `TeamLine.games`, `minutes` (clock), `possessions`, `pts`, `opp_pts` are unchanged, so `compute` and the Cavaliers reproduction are untouched apart from constructor arguments.
- `season_lines` fills both boxes from the per-game sums it already builds (`boxes[team_id]`, `boxes[opp_id]`).

`derive/value.py`:

- Pure functions over `PlayerLine`/`TeamLine`: a `win_shares(line, team, league) -> StintWinShares(player_id, team_id, minutes, ows, dws)` (name free), and a `league_context(teams) -> LeagueContext(pts, possessions, team_games, pace)`. Nothing in them touches the database.
- `ws_rows(day, stints) -> list[ImpactSnapshotRow]`: one row per player with `MIN_MINUTES` or more across his stints: `value = round(Σ ows + Σ dws, 3)`, `o_value = round(Σ ows, 3)`, `d_value = round(Σ dws, 3)`, `minutes_basis = round(Σ minutes, 1)`. Sorted by player, rounded so a re-run writes nothing (`replace_snapshot`).
- `compute_value(db, day)` calls `season_lines(session, season, day)` once, writes the VORP rows as now and the WS rows in place of the current clearing of `ws_local`; its return dict gains a count of WS rows. The module docstring states every formula above and its inputs, and the departures.

No migration, no change to `derive/run.py`, `IMPACT_PREFERENCE`, `ImpactSeries`, the view, or the API.

## Fixture

`backend/tests/fixtures/bref/`:

- `2025-26_ws.csv`: `bref_id, team, ows, dws, ws`, one row per row of `2025-26_players.csv` in its order, from the 2026 Advanced page's regular-season table (`OWS`, `DWS`, `WS`), joined on Basketball Reference ID and team; every row's `mp` on the page matched the players file's.
- `2025-26_teams.csv` gains `opp_ft` (opponents' free throws made) from the season page's Opponent Stats table. Team `fg3`, `ft`, `ast`, `stl`, `blk` and `pf` are the players' sums per the README, computed in the test from `2025-26_players.csv`; `tov` stays the team's, team turnovers included, so the fixture check is of the formulas over the published totals.
- `README.md` gains a paragraph for the new file and column: pages, the date read, the parser being throwaway and outside the repo, and the join check.

## Tests (`tests/test_value.py`, built like `tests/test_bpm.py`)

- A `bref_season`-style loader (extend `tests/test_bpm.py`'s, which must then fill the new `TeamLine` fields) builds `PlayerLine`s and `TeamLine`s with both boxes from the fixture.
- `test_every_regulars_win_shares_are_within_the_published_value`: for the 263 player-team rows with `mp ≥ 1000`, `ows`, `dws` and `ws` each within ±0.5 of the fixture; the count of regulars asserted as 263.
- `test_league_win_shares_total_within_three_percent_of_league_wins`: Σ WS over all 661 rows within 3% of 1,230.
- A hand-computed unit test of the pure function over one small stint and team (one player, a team of a few lines), so a slip in a constant is caught independently of the fixture; and one for each zero guard.
- `test_ws_rows_*`: a traded player's two stints sum into one row with his minutes summed; a player under a minute gets no row; rows rounded.
- The seeded `compute_value` test: updated for `ws_local` rows now being written. Either the filler teams get nonzero box lines so the three players have WS rows (values asserted equal to the pure function over `season_lines`' output for the seeded data, `value == o_value + d_value`, `minutes_basis` his minutes), or the test asserts the no-possessions guard yields no WS rows and a second small scenario covers the written case. A re-run writes nothing; the stale `ws_local` row for a player without a line is removed.
- `tests/test_bpm.py`: unchanged assertions; constructors updated.

## Acceptance criteria

- `uv run pytest tests/test_value.py` passes the WS tolerances over the 2025-26 fixture, and `tests/test_bpm.py` still passes.
- After `derive` for a seeded date, every player with a minute or more in the season through the date has a `ws_local` row with `o_value`, `d_value` and `minutes_basis`, and `value = o_value + d_value`; a second run changes nothing.
- `derive/value.py`'s docstring states every formula and input listed above and the departures; `grep -ri basketball-reference backend/src` finds only docstring citations, no request.
- `tests/fixtures/bref/README.md` records the WS file's and `opp_ft` column's provenance.
- `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` pass in `backend/`.

## Manual step after merge (not Done until then)

Run the value step for the prior season's final date once more (the NEU-1996 `derive --date` path) so the prior-season window carries WS before NEU-1998 reads it; the current season's dates fill in nightly, and `derive --missing` does not re-run dates whose `value` step already succeeded.

## Out of scope

PER; the view columns (NEU-1998); the production table and tier board (NEU-1998, NEU-1999); team turnovers from play-by-play; WS over `stint` possessions; any Basketball Reference request.

# Basketball Reference fixtures

The 2025-26 regular season as Basketball Reference publishes it, trimmed to what `derive/bpm.py`
and `derive/value.py` read and the published BPM, VORP and Win Shares they are checked against
(`tests/test_bpm.py`, `tests/test_value.py`). Not recorded by `scripts/record_fixtures.py`, which records NBA.com only;
the tables were read out of the pages with a throwaway parser on 2026-10-23 and written as CSV.

- `2025-26_players.csv`: one row per player per team (a traded player's `2TM` total is left
  out), from the Totals page's box-score columns and the Advanced page's `OBPM`, `DBPM` and
  `BPM`, joined on the player's Basketball Reference ID and team:
  https://www.basketball-reference.com/leagues/NBA_2026_totals.html and
  https://www.basketball-reference.com/leagues/NBA_2026_advanced.html. 661 rows, 30 teams.
  `mp` is the player's minutes as listed, rounded, so a team's sum is within ten of its `mp`.
- `2025-26_teams.csv`: one row per team, from the season page's Team Stats, Opponent Stats and
  Advanced Stats tables: https://www.basketball-reference.com/leagues/NBA_2026.html. `mp` is the
  five players' minutes together (five times the game clock). `tov` is the team's, team turnovers
  included, so it exceeds its players' sum; every other total is its players' sum exactly.
  `opp_ft`, the opponents' free throws made, was added from the same page's Opponent Stats table
  with a throwaway parser on 2026-10-27, for Win Shares; that read's `opp_fg`, `opp_fga`,
  `opp_fta`, `opp_orb`, `opp_drb`, `opp_tov` and `opp_pts` matched the file's for every team. The
  team totals Win Shares reads that are not columns here (three-pointers, free throws, assists,
  steals, blocks, fouls) are its players' sums, taken in the test.
- `2025-26_vorp.csv`: the published VORP for each row of `2025-26_players.csv`, in its order,
  from the same Advanced page's `VORP` column (its regular-season table, not the playoffs'), read
  out with a throwaway parser on 2026-10-27 and joined on Basketball Reference ID and team; every
  row's `mp` and `bpm` on the page matched the players file's. Checked by `tests/test_value.py`.
- `2025-26_ws.csv`: the published offensive, defensive and total Win Shares for each row of
  `2025-26_players.csv`, in its order, from the same Advanced page's `OWS`, `DWS` and `WS` columns
  (its regular-season table), read out with a throwaway parser outside the repo on 2026-10-27 and
  joined on Basketball Reference ID and team; every row's `mp`, `bpm` and `vorp` on the page
  matched the players and VORP files'. Checked by `tests/test_value.py`.
- `2016-17_cavaliers_sheet.csv`: the worked example in the BPM 2.0 calculator sheet linked from
  https://www.basketball-reference.com/about/bpm2.html (its `CLE 2017` tab, exported as CSV on
  2026-10-23): each Cavalier's totals and listed position on the five-point scale, then the
  sheet's position, offensive role, BPM and OBPM for him, each rounded as the sheet shows it.
  The team inputs the sheet used (19881 minutes, a pace of 96.2, 9048 points, a +3.0 rating and
  an offensive rating 4.8 above the league's) are in `tests/test_bpm.py`.

Team codes are Basketball Reference's (`BRK`, `CHO`, `PHO`); the tests never map them to NBA
IDs.

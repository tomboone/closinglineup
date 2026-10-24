# Basketball Reference fixtures

The 2025-26 regular season as Basketball Reference publishes it, trimmed to what `derive/bpm.py`
reads and the published BPM it is checked against (`tests/test_bpm.py`). Not recorded by
`scripts/record_fixtures.py`, which records NBA.com only; the tables were read out of the pages
with a throwaway parser on 2026-10-23 and written as CSV.

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
- `2016-17_cavaliers_sheet.csv`: the worked example in the BPM 2.0 calculator sheet linked from
  https://www.basketball-reference.com/about/bpm2.html (its `CLE 2017` tab, exported as CSV on
  2026-10-23): each Cavalier's totals and listed position on the five-point scale, then the
  sheet's position, offensive role, BPM and OBPM for him, each rounded as the sheet shows it.
  The team inputs the sheet used (19881 minutes, a pace of 96.2, 9048 points, a +3.0 rating and
  an offensive rating 4.8 above the league's) are in `tests/test_bpm.py`.

Team codes are Basketball Reference's (`BRK`, `CHO`, `PHO`); the tests never map them to NBA
IDs.

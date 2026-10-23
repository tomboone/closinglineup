# Reconciliation fixtures

NBA.com's per-game responses for each team of three games, filtered to the game's date alone with
`DateFrom` and `DateTo`, each as `{team_id: response}`:

- `<game_id>_leaguedashlineups.json`: `leaguedashlineups` (Advanced, `GroupQuantity` 5,
  `PerMode` Totals, `TeamID` the team's).
- `<game_id>_leagueplayerondetails.json`: `leagueplayerondetails` (Advanced, `PerMode` Totals,
  `TeamID` the team's).

The games are `0022500006` (DEN at GSW, the overtime game), `0022500064` (MIN at OKC, the
ordering-quirk game) and `0022500270` (ATL at NOP), whose play-by-play is in `../pbp/`. They pin
how NBA.com credits possessions to lineups and players within a game, which the season-wide
fixtures cannot (NEU-1899, NEU-1900): every five and every player in them has NBA.com's `POSS`
exactly.

`0022500006_leaguedashlineups.json` was recorded 2026-10-22 at about 21:55 ET from kumadrome; the
rest on 2026-10-22 at about 22:15 ET from the dev Mac on the home network, which reproduced that
file byte for byte. Both used the session `sources/nba_http.py` opens, with
`nba_stats.LEAGUE_DASH_FILTERS` (`leaguedashlineups`) or `nba_stats.DASH_FILTERS`
(`leagueplayerondetails`), plus the two dates in `MM/DD/YYYY`. `record_fixtures.py` does not
record them; nothing in the app fetches a single date.

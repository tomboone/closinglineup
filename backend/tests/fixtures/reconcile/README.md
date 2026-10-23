# Reconciliation fixtures

`0022500006_leaguedashlineups.json` is NBA.com's `leaguedashlineups` (Advanced, `GroupQuantity`
5, `PerMode` Totals) for each team of game 0022500006, DEN at GSW on 2025-10-23, filtered to that
date alone with `DateFrom` and `DateTo`: `{team_id: response}`. It pins how NBA.com credits
possessions to lineups within one game, which the season-wide fixture cannot (NEU-1899).

Recorded 2026-10-23 at about 21:55 ET from kumadrome, with the session `sources/nba_http.py`
opens and `nba_stats.LEAGUE_DASH_FILTERS` plus the two dates. `record_fixtures.py` does not
record it; nothing in the app fetches a single date.

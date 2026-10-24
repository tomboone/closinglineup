# Derive case fixture

Real 2025-26 data for the hand-labelled cases in `tests/derive/cases/*.yml`, which
`tests/test_derive_cases.py` loads into a database of its own and derives over. Five teams, ATL,
DET, LAL, OKC and PHI, every one of their final games from opening night through 2026-03-20, the
last case's date. Chosen for what they hold between them: two trades with both sides here (Jared
McCain PHI → OKC, Luke Kennard ATL → LAL, Gabe Vincent the other way), two-way players near the
limit (OKC, PHI, DET), the most G League assignments (DET, ATL, PHI), and every flag.

One gzipped CSV per table, header first, every column but `id`, `created_at` and `updated_at`,
loaded in this order:

| File | Rows | What |
|---|---|---|
| `player.csv.gz` | 646 | every player with a box-score line or a roster span on the five |
| `game.csv.gz` | 329 | the five's games, both teams named |
| `player_game.csv.gz` | 4,518 | the five's box-score lines only, so no opponent gets a `rotation_state` row |
| `stint.csv.gz` | 21,304 | both teams' stints of those games: a stint's defensive possessions are the other team's (`db/views.sql`) |
| `roster_membership.csv.gz` | 964 | every span on the five, two-way and ten-day contracts included |
| `injury_status.csv.gz` | 2,517 | the five's lines on one official report a game date |
| `transaction.csv.gz` | 340 | those players' NBA.com player-movement rows, 2025-07-01 to 2026-03-20 |

## How it was built (2026-10-23)

1. `player`, `game`, `player_game` and `stint` were copied whole from kumadrome's Postgres into a
   scratch database on the dev Mac.
2. kumadrome had no 2025-26 overlays then, so the rest was fetched on the dev Mac through the
   project's own code: `overlays transactions` (NBA.com's player movement), `fetch --rosters
   --season 2025-26`, `load --memberships` (roster spans with their contract types), and, for
   every 2025-26 game date, the official injury report of about 5:30 PM Eastern, parsed and
   resolved as the `injuries` overlay does, without its ESPN fallback. Reports before about
   2025-12-22 are stamped by the hour (`Injury-Report_2025-12-10_05PM.pdf`), a form
   `injury_pdf.discover` does not try, so those were fetched by that URL. 209 of the season's 212
   game dates have a report; the three without are playoff dates in May, after the cases. A few
   lines a report name a player the name index cannot resolve and have no row, as in production.
3. `derive` ran over the whole regular season, and the candidates were picked from what it
   wrote, to cover each tier, flag and roster move.
4. Each label was then read off the box scores and reports, not off `derive`: a throwaway script
   (not kept) applied spec §7.5 and §7.7 as written to the raw rows, and every case's tier and
   flags are what it gave, which matched `derive` on all of them. So the suite pins `derive` to
   the spec's reading of real games; it found no disagreement to exclude or file. The `note` of
   each case gives the evidence to check a label by hand: game IDs (NBA.com's box score is
   `https://www.nba.com/game/<game ID>`), report file names, and the numbers behind each tier and
   flag.
5. The five teams' rows were exported to these files.

The fixture gives the same rotation state, events and flags for the case players as the full
league does: role windows read only the team's own games, and production windows (usage, TS%)
only the player's, both of whose teams are here for every traded case player. A player traded
in from a team not here, whose production windows miss those games, is not a case.

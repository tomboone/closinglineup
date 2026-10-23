# ESPN injuries fixtures

One response of ESPN's injuries endpoint, laid out like the raw cache
(`injuries/<instant fetched, UTC>.json`), from
`https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries?season=2027&seasontype=2`.

Fetched 2026-10-23 at 14:45:50Z (its `timestamp`) with plain curl, and trimmed: each injury keeps
its `id`, `date`, `status`, `type` and `details`, and its athlete only `displayName`, `firstName`,
`lastName` and `team` (`id`, `abbreviation`, `displayName`); the long RotoWire notes, links and
headshots are dropped. Not by `scripts/record_fixtures.py`, which records NBA.com only.

The counts the tests assert were taken from the JSON's fields, not from the parser:

| Lines | `Out` | `Day-To-Day` | Teams (by each athlete's own team) |
|---|---|---|---|
| 79 | 50 | 29 | 25 |

Brandon Ingram is listed under the LA Clippers' group (ESPN 12) with his own team Toronto (28),
as on 2026-10-20 when nba-today recorded its fixture; the parser keeps the athlete's team.

`players.json` is `[nba_player_id, name]` for every player the response names, from NBA.com's
2026-27 `commonallplayers` (`IsOnlyCurrentSeason=0`), fetched the same day; each name matches
exactly one player there.

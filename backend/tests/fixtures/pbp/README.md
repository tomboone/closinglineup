# pbpstats fixtures

The cdn.nba.com live play-by-play and box score of each full game `parse/pbp.py` is tested on
(spec §11), laid out like the raw cache (`<endpoint>/<key>.json`) and kept whole:

- `0022500006`, DEN at GSW on 2025-10-23, which GSW won 137-131 after one overtime: the same game
  as the stats.nba.com per-game fixtures.
- `0022500064`, MIN at OKC on 2025-11-26, which OKC won 113-105: the ordering-quirk game. It logs
  19 substitutions between free throws, a late-game inbound swap of a ten that plays no time but
  turns the ball over, and an end-of-quarter `heave` action, which pbpstats 1.3.11 does not
  recognise.
- `0022600003`, OKC at SAS on 2026-10-20, which OKC won 116-104: a 2026-27 feed. Its 48 actions
  with no player (period starts and ends, timeouts, team rebounds) have no `personId`, and
  rebound 599 is listed just before the missed three (598) it follows (NEU-1897).

The box scores supply each game's final score, which the stints' points must add up to. Re-record
them from `backend/` on a residential IP (kumadrome, or a machine on the same network):

```
uv run scripts/record_fixtures.py pbp
```

This replaces every file here with the games in `PBP_GAME_IDS`.

Recorded 2026-10-21 at about 20:45 ET, from the dev Mac on the home network.
`0022600003` was added 2026-10-22 from kumadrome's raw cache rather than recorded, so that it is
the feed the failure was seen on: the cache's files, written out in the recorder's format.

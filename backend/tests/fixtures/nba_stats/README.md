# stats.nba.com fixtures

One response per endpoint, laid out like the raw cache (`<endpoint>/<key>.json`).

## Per-game endpoints

All eight per-game endpoints for game 0022500006, DEN at GSW on 2025-10-23, which GSW won 137-131
after one overtime. Every response is kept whole, overtime included, because the per-game parsers
and pbpstats are tested on a full game (spec §11). Re-record them from `backend/` on a residential
IP (kumadrome, or a machine on the same network):

```
uv run scripts/record_fixtures.py stats [--game-id GAME_ID]
```

This replaces each endpoint's file with that game's response. If an endpoint fails, the recorder
skips it and keeps the existing file. Tests that pin the game ID need updating to match.

Recorded 2026-10-21 at about 20:13 ET, from the dev Mac on the home network.

`playbyplayv2/0022500006.json` is `{}`, exactly what stats.nba.com returned. The endpoint answers
`{}` for every game tried, 2024-25 and 2025-26 alike, whatever the parameters. The fetcher refuses
to cache that response, so the recorder cannot re-record it; it was written by hand from a probe
on the same evening.

## Schedule

`scheduleleaguev2/2025-26.json` is the 2025-26 schedule in the cdn.nba.com shape. It was recorded
for the season helpers (NEU-1849) and covers the All-Star, play-in and playoff phases.

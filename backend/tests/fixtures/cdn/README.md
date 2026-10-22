# cdn.nba.com fixtures

One response per endpoint, laid out like the raw cache (`<endpoint>/<key>.json`); the schedule
and play-by-play are trimmed, the scoreboard and box score kept whole.
Re-record them from `backend/` on a residential IP (kumadrome, or a machine on the same network):

```
uv run scripts/record_fixtures.py cdn [--game-id GAME_ID]
```

This replaces every file here: this season's schedule (the first game of each kind), the
scoreboard as of now, and the live box score and play-by-play of `--game-id` (default: the latest
final game). Tests that pin a game ID or a schedule game need updating to match.

Recorded 2026-10-21 at about 20:03 ET, from the dev Mac on the home network, for game 0022600003:
early in the evening, so the scoreboard's games are scheduled or in progress, none final. The
2026-27 schedule lists no All-Star, play-in or playoff games yet; the 2025-26 stats.nba.com
schedule fixture in the same shape covers those phases.

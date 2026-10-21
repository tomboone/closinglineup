---
status: accepted
---

# Rolling windows are views; rotation state and events are nightly tables

The handoff's rule is "rolling-window stats are views, never stored rows", and that holds for `player_window_stats`: it is a materialized view refreshed after each nightly load, so a metric definition change is one migration away from the UI. Rotation state is the exception. A player's tier on a date depends on the availability that was current on that date, and a rotation event is the diff between two consecutive dates; computing either on the fly means joining point-in-time injury snapshots and self-joining across dates on every morning-page request, on a 2011 Mac mini. So a deterministic, idempotent `derive` step writes one `rotation_state` row per player-team-game-date and the `rotation_event` rows between them, for any date range, after every load. The hand-labelled tier tests in M3 run that same step over fixtures.

## Consequences

- Changing a tier threshold or a flag rule requires re-running `derive` over history; the command takes a date range so that is a one-liner.
- Tier inputs (minutes share, starter rate, availability, window length) are stored on the row so an event can show what moved it without recomputing.

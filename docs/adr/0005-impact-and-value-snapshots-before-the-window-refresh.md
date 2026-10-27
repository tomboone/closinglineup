# Impact and value snapshots are written before the window refresh

`player_window_stats` reads `impact_snapshot` (local BPM, Win Shares, VORP) along with the box scores, and is refreshed once per nightly `derive` run. Until NEU-2003 the run refreshed the view first and wrote that date's BPM and value snapshots after it, so the view's newest date always carried the previous date's snapshots until the next night. A season total one game stale is barely visible; a windowed total is not: a 5-game window's Win Shares would hold about four games' worth against five games' minutes.

So `derive` writes BPM and the value metrics for each date first, then refreshes the view, then computes tiers, events and flags. Neither BPM nor the value step reads the view or anything derived after it (`game`, `player`, `player_game`, `roster_membership` and `impact_snapshot` only), so the order costs nothing and the run still refreshes once. A second refresh at the end was rejected: it doubles the run's slowest step to fix an ordering.

Keep it this way: a step that writes something the view reads runs before `WINDOWS`; a step that reads the view runs after it.

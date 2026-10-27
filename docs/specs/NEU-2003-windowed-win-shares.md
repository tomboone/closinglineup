# NEU-2003: Windowed Win Shares and WS/48

Ticket: NEU-2003 (story NEU-2002, milestone M5 Player-centred views). Project spec: `closinglineup-v2-morning-feed-and-home-grown-metrics-project-spec.md` §15.1, building on §8.2–8.6. Decision record: ADR 0005.

## Why

Win Shares are shown only as season-to-date and prior-season totals, so a player's value over his last 5, 10 or 20 games, the recent form every other production metric shows, can't be read anywhere, and there is no rate to compare a starter with a reserve. The data is already there: `impact_snapshot` holds a season-to-date `ws_local` per player per date, so a window's Win Shares are the difference between two of them.

While working this out we found the nightly run refreshes `player_window_stats` before it writes that date's BPM and value snapshots. The view's newest date therefore always carried the previous date's BPM, Win Shares and VORP. That is barely visible on a season total but wrong for a window: a 5-game window would hold about four games of Win Shares against five games' minutes. This ticket fixes the order (ADR 0005).

## What to build

### 1. Step order in `derive/run.py`

For each date, `BPM` then `VALUE` run before the single `WINDOWS` refresh. `TIERS`, `EVENTS` and `FLAGS` run after it, per date, as now:

```
for day: GARBAGE_TIME
for day: BPM, VALUE        # VORP reads the date's BPM
WINDOWS (once, all dates)
for day: TIERS, EVENTS, FLAGS
```

Neither `write_bpm` nor `compute_value` reads the view, the tiers or garbage time. Their inputs are `game`, `player`, `player_game`, `roster_membership` and `impact_snapshot`. `WRITES`' membership is unchanged. Update the module docstring's description of the order.

### 2. The window view (`db/views.sql` and a migration recreating it)

Production rows (team_id NULL) gain:

| Column | 5 / 10 / 20 | season | prior_season |
|---|---|---|---|
| `ws_window` | `ws_local` at the date less `ws_local` n production games earlier (`lag(…, n) OVER back`); `ws_local` itself while `k <= n` | `ws_local` | the prior season's final (`value_final`) |
| `ws_window_o`, `ws_window_d` | the same, over `ws_local_o` / `ws_local_d` | `ws_local_o` / `_d` | final `_o` / `_d` |
| `ws_48` | `ws_window * 2880 / nullif(seconds, 0)` | same | same, over the prior season's seconds |
| `ws_48_delta_prior` | as every windowed metric: the window's value less its own n games back | less prior_season's | NULL |
| `ws_48_delta_season` | less the season window's at the same date | 0 | less the season window's, as every metric's |

- **Lag over `k`.** `k` is the player's production game count, his teams' games, across spells, one per date. Every production window counts it, and a traded player's `ws_local` snapshot is already summed over his teams, so the difference is right across a trade.
- **Missing start snapshot → NULL.** The view already carries the latest snapshot forward within the season, so a single missed night is covered. Only when `k > n` and no snapshot exists at or before the window's start (the value step began partway into the season) is the start NULL, and then `ws_window` is NULL, not the season to date. Before the season's first game (`k <= n`) the start counts as 0.
- **`seconds` in production rows.** Today `prod_metric` drops `seconds`, which `prod_window` has. Carry it through `prod_metric` and the prior_season branch of `prod_all` (`t.seconds`) so `ws_48` can use it. It need not become an output column.
- `ws_local`, `ws_local_o`, `ws_local_d` and `vorp_local` keep their meaning: the season-to-date snapshot in every window. The tier board (NEU-1999) reads them.

`tests/test_windows.py`'s check that the migration's copy of the view matches `views.sql` keeps the two the same.

### 3. API (`api/routers/players.py`, `api/schemas.py`)

- The Win Shares metric reads `ws_window` in every production window, with `delta_season` null. A window's total and the season's don't compare. Give `_Metric` a flag for "no delta", so `_production_table` doesn't ask for a `ws_window_delta_season` column. Its OWS/DWS hover reads `ws_window_o` and `ws_window_d`.
- A new `ws_48` metric, label "WS/48", format `rate`, follows Win Shares in every window, with its delta. `ProductionMetric` admits `ws_48`.
- VORP is unchanged: season and prior only.
- Before his first game of the season, the prior-season override (`_final_values`) fills `ws_window`, `_o` and `_d` from the final snapshots, as it does `ws_local` now. `ws_48` comes from the prior season's season-window row the function already reads.
- Update `_production_table`'s docstring: Win Shares carry no delta, WS/48 does, VORP is season-only.

OpenAPI snapshot and `frontend/src/api/types.ts` regenerated.

### 4. Frontend

`ProductionTable` needs no new code path. `rate` formats as `.123` with a `+.012` delta, as Basketball Reference prints WS/48, and Win Shares cells now fill in Last 5/10/20. Extend the player page's production test.

## Acceptance criteria

- `tests/test_windows.py`, a player with season-to-date WS snapshots of 1.0 after game 5 and 2.5 after game 15:
  - his 10-game window at game 15 has `ws_window` 1.5 and `ws_48` = 1.5 × 2880 / that window's seconds;
  - at game 8 his 10-game `ws_window` is the season to date;
  - a player whose first snapshot comes after game 10 has NULL `ws_window` in his 5-game window at game 15.
- `prior_season` rows carry the prior season's final `ws_window` and its `ws_48`.
- After `derive` runs a date, the view's row on that date carries that date's `bpm_local` and `ws_local` snapshots, not the previous date's (`tests/test_run.py`, or wherever the run order is tested).
- `GET /api/players/{id}` production:
  - the `ws` row has values in windows 5, 10 and 20, each with `delta_season` null and windowed `o_value` / `d_value`;
  - a `ws_48` row follows it in every window, with deltas.
- The player page shows a WS/48 row under Win Shares, formatted `.xxx`.
- Both CI gates pass.

## Out of scope

- Windowed VORP. Its trend is BPM's and minutes share's, which the page already charts.
- A WS or VORP trend chart.
- The tier board's season columns, which keep reading `ws_local` / `vorp_local`.
- The league table (NEU-2006), which reads `ws_window` and `ws_48` once this ships.
- The refresh's cost on kumadrome: asserted, not measured. The reorder adds no refresh.

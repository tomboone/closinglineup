# pbpstats overrides

Per-game corrections for play-by-play that `parse` cannot turn into stints as it comes. Each entry
is a scorer's error or a gap in the feed, checked against the actions around it; `parse/pbp.py`
says how each file is read.

## `missing_period_starters.json`

An overtime's starters when one of them logs no event in the period, so pbpstats cannot infer
him. Each is the team's five at the end of the fourth, with the substitutions logged before the
overtime's start event applied; the substitutions run consistently through every period of the
game.

| Game | Date | Team | Starter with no overtime event |
| -- | -- | -- | -- |
| `0022500458` | 2025-12-30 | PHI `1610612755` | Quentin Grimes `1629656` |
| `0022500458` | 2025-12-30 | MEM `1610612763` | Santi Aldama `1630583` |
| `0022500521` | 2026-01-07 | BKN `1610612751` | Noah Clowney `1641730` |
| `0022500550` | 2026-01-11 | PHI `1610612755` | Adem Bona `1641737` |
| `0022500637` | 2026-01-23 | BOS `1610612738` | Sam Hauser `1630573` |
| `0022501029` | 2026-03-21 | DAL `1610612742` | P.J. Washington `1629023` |
| `0022501044` | 2026-03-23 | DAL `1610612742` | P.J. Washington `1629023` |

## `action_overrides.json`

An action's own fields, which pbpstats has no override for: `{game_id: {action_number: {field:
value}}}`, applied by `materialize` as it writes the feed.

| Game | Date | Action | Change | Why |
| -- | -- | -- | -- | -- |
| `0022500449` | 2025-12-29 | 578, lane violation | `clock` 9:30 → 9:27 | Listed after the defensive rebound (580) at 9:27 that follows the missed free throw it belongs to, so the clock ran backwards |
| `0022501031` | 2026-03-21 | 322, end of 2nd | `clock` 0:00.3 → 0:00.0 | The period ends after a heave (320) and team rebound (321) at 0:00.0 |

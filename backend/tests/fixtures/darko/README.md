# darko.app fixtures

One copy of the DPM leaderboard's data file, laid out like the raw cache
(`leaderboard/<date fetched>.json`), from `https://www.darko.app/__data.json`: the SvelteKit route
data behind the leaderboard's "Download CSV" button, which serializes the board in the browser
(`sources/darko.py` documents the file's devalue layout and columns).

Fetched 2026-10-23 with plain curl, and kept whole: devalue's flat list addresses every value by
its index, so a trimmed copy would have to be re-serialized, and at 166 KB there is little to
trim. Not by `scripts/record_fixtures.py`, which records NBA.com only.

The counts the tests assert were taken from the file's columns, not from the parser:

| Lines | With `nba_id` | Distinct `nba_id` | Teams (`tm_id`) | `season` | `ratingsThrough` |
|---|---|---|---|---|---|
| 519 | 519 | 519 | 30 | 2027 | 2026-10-22 |

The first line is Nikola Jokic (203999, DEN 1610612743): `dpm` 7.0854, `o_dpm` 5.13644, `d_dpm`
1.94895, `x_minutes` 32.6348.

# Injury report fixtures

Three official NBA injury reports, kept whole as published, laid out like the raw cache
(`report/<report time, Eastern>.pdf`), from `https://ak-static.cms.nba.com/referee/injury/`:

| File | Report | Why |
|---|---|---|
| `report/2026-10-22T1700.pdf` | Thursday 2026-10-22, 5:00 PM | A weekday: the evening's games, then the next day's, where some teams, SAS among them, had not yet submitted while their opponents had |
| `report/2026-03-01T1715.pdf` | Sunday 2026-03-01, 5:15 PM | A weekend, nine pages: reasons cut by page breaks, "Yang, Hansen" (an alias) and Tamar Bates (not in NBA.com's player list) |
| `report/2026-10-22T0900.pdf` | Thursday 2026-10-22, 9:00 AM | The morning: the day's games submitted, every one of the next day's teams "NOT YET SUBMITTED" |

Downloaded 2026-10-23 from the dev Mac on the home network, by URL, e.g.
`Injury-Report_2026-10-22_05_00PM.pdf`; `sources/injury_pdf.py` documents the pattern. Not by
`scripts/record_fixtures.py`: a report is a PDF kept whole, with nothing to trim, so a new one is
a download into `report/` under its key, and the hand counts below re-taken.

The hand counts the tests assert were taken from each PDF's words, not from the parser: every
status word in the Current Status column, and every "NOT YET SUBMITTED".

| File | Lines | Out | Questionable | Doubtful | Probable | Available | Not yet submitted |
|---|---|---|---|---|---|---|---|
| `report/2026-10-22T1700.pdf` | 63 | 36 | 16 | 2 | 6 | 3 | 8 teams |
| `report/2026-03-01T1715.pdf` | 134 | 106 | 10 | 2 | 1 | 15 | 6 teams |
| `report/2026-10-22T0900.pdf` | 12 | 9 | 3 | 0 | 0 | 0 | 24 teams |

`players.json` is `[nba_player_id, name]` for every player the three reports name that NBA.com's
2026-27 `commonallplayers` (`IsOnlyCurrentSeason=0`) lists, fetched the same day: the rows
`commonallplayers` gives `player.name`, trimmed to these players.

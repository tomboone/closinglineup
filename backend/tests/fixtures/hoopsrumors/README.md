# Hoops Rumors fixtures

One copy of each of Hoops Rumors' two 2026-27 contract articles, laid out like the raw cache
(`<endpoint>/<date fetched>.html`):

- `non_guaranteed/`:
  `https://www.hoopsrumors.com/2026/07/2026-27-non-guaranteed-contracts-by-team.html`
- `guarantee_dates/`:
  `https://www.hoopsrumors.com/2026/05/early-nba-salary-guarantee-dates-for-2026-27.html`

Fetched 2026-10-23 with plain curl, a request at a time per robots.txt's `Crawl-delay: 1`, and
kept whole, so the parser reads the page as the overlay would, sidebar and comments included
(`sources/hoopsrumors.py` documents the layout). Not by `scripts/record_fixtures.py`, which records
NBA.com only.

The counts the tests assert were taken from the HTML, not from the parser.

**Non-guaranteed list** (`Updated 10-22-26 (7:08 am CT)`): 30 team headings, 15 of them `None`
and 15 listing 22 players. Eleven players have a partial guarantee. Only Duncan Robinson (DET)
has a salary: $15,992,957, partially guaranteed for $2,000,000. Tolu Smith (DET) has neither.
The page spells the Clippers "Los Angeles Clippers", and Jae'Sean Tate with a curly apostrophe.

**Guarantee dates**: 38 lines. Of these, 29 are ✅ and 7 are ❌, so 36 are settled. Six fall under
"First day of NBA regular season" and four under "First day of team's regular season". The two
unsettled lines:

| Date | Player | Line |
|---|---|---|
| November 15 | Mario Hezonja (Cavaliers) | Non-guaranteed salary ($2,845,883) becomes fully guaranteed. |
| December 30 | Trendon Watford (Pelicans) | Partial guarantee ($500,000) increases to full guarantee ($2,845,883). |

Seven lines are for a player and team on the list: Mo Bamba (Jazz) ×2, Isaiah Crawford, Brandon
Williams, Trendon Watford ×2 and Mario Hezonja. The other 31 are a run's `ignored_lines`.

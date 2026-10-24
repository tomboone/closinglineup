# NEU-1873: non-guaranteed contracts from Hoops Rumors, weekly

Linear: [NEU-1873](https://linear.app/neuroticsasquatch/issue/NEU-1873) · Story NEU-1844 · Milestone M4 Overlays
Related: NEU-1866 (snapshot tables, merged: created `contract`), NEU-1878 (flags, backlog: reads `contract` for `contract_pressure`), NEU-1871 (DARKO, merged: the overlay this one is shaped like)
Decision record: `docs/adr/0004-contracts-from-hoops-rumors.md`

## What and why

Contract pressure drives rotation decisions: a team plays, or cuts, a player on a non-guaranteed
deal before his salary guarantees. Spec §7.7's `contract_pressure` flag (NEU-1878) needs, per player,
"non-guaranteed money with a guarantee date within 30 days". Nothing captures it.

The project spec named Spotrac team cap pages with a RealGM fallback. The terms check this ticket
called for (comment on NEU-1873, 2026-10-23) rules out both. Spotrac's Terms of Service forbid
"any spider, robot, cheat utility, scraper", and RealGM's require all use to be "click-by-click
selections directly by a human being". Both also answer non-browser clients with 403. ESPN (Disney's terms
forbid scraping) and HoopsHype (Spotrac's owner, 402 to a plain client) are out too. So is
the gabriel1200/site_Data repo, which is unlicensed and itself a Spotrac/RealGM scrape.

Hoops Rumors publishes exactly the facts the flag needs. Its robots.txt allows the articles (`User-agent: *`,
`Crawl-delay: 1`), it publishes no terms of use (only a privacy policy), and plain httpx gets 200.
Each season it maintains two articles until January 10:

- **"<season> Non-Guaranteed Contracts By Team"** lists every player without a fully guaranteed
  salary under team headings: non-guaranteed, partially guaranteed, Exhibit 9 and Exhibit 10. The list is
  "minimum-salary contracts unless otherwise noted". Two-way contracts are not on it.
- **"Early NBA Salary Guarantee Dates For <season>"** lists, under date headings, the players whose
  guarantee triggers before the league-wide default, with the amounts before and after.

So `contract` narrows to **non-guaranteed contracts**: a player absent from the list is taken as
fully guaranteed. Two-way status stays `roster_membership`'s, which the milestone makes the single
source of contract-type badges.

## Decisions

1. **Source: the two Hoops Rumors articles, weekly.** The overlay is `contracts`, in the
   `weekly` family (Mon 04:30, spec §3.3), behind `HOOPSRUMORS_ENABLED` (it replaces
   `SPOTRAC_ENABLED`, default `false`). Off, the run is a no-op with no request. There is no fallback:
   RealGM is dropped. A fetch or parse failure raises, the weekly job pings `/fail`, and the last
   snapshot stays.
2. **URLs: a committed season table.** `sources/hoopsrumors.py` holds
   `ARTICLES: dict[str, Articles]`, season label → `(non_guaranteed, guarantee_dates)` URLs, edited
   once a year when the articles appear (May–July). The URLs move each season and the month in the
   path varies; robots.txt disallows `/search` and `/*?s`, so there is no lookup. A season with no entry
   raises `HoopsRumorsError("no Hoops Rumors articles for 2027-28; add them to ARTICLES")`. The 2026-27 entries:
   - `https://www.hoopsrumors.com/2026/07/2026-27-non-guaranteed-contracts-by-team.html`
   - `https://www.hoopsrumors.com/2026/05/early-nba-salary-guarantee-dates-for-2026-27.html`
3. **Fetch and cache.** Fetching uses plain httpx through `Jobs.http` (`sources/web.py`), with no curl_cffi,
   and the two requests are at least 1 s apart. Each page is cached as
   `raw/hoopsrumors/{non_guaranteed,guarantee_dates}/<run date>.html` (`RawCache` with
   `suffix=".html"`), so a re-run the same day reads the cache. Only a 200 whose body has the
   article's `<h1 class="entry-title">` is cached; anything else raises. As DARKO does, a first run for
   a date other than today (in `TZ`) raises rather than file today's page under it.
4. **Parsing the non-guaranteed list** (`parse_non_guaranteed`). Within the article body
   (`entry-content`):
   - Each `<h3>` is a team's full name (`teams.by_name`). A heading that names no NBA team raises.
   - Under it, `None` means no players. Otherwise each player line is `Name` or `Name ($15,992,957)`, optionally
     followed by `Partially guaranteed for $2,000,000.`
   - Each line yields a `NonGuaranteedLine(team_id, player, salary: int | None, partial: int | None)`.
   - The `Updated 10-22-26 (7:08 am CT)` stamp is parsed as `updated: date | None`.
   - A body with fewer than 30 team headings raises, because the layout has changed.
5. **Parsing the guarantee dates** (`parse_guarantee_dates`).
   - **Headings.** A heading is a date, `June 28`: June–December are in the season's first year,
     January–May in its second. Two named headings also occur: `First day of NBA regular season`
     resolves to the season's first counted `game` date, and `First day of team's regular season` to
     the line's team's first counted game of the season.
   - **Lines.** Each line is `Name (Team): <before> ($X) <becomes|increases to> <after> ($Y).`, optionally
     ending `(✅)` (guaranteed on that date) or `(❌)` (waived before it). `<after>` is `fully
     guaranteed`, `full guarantee ($Y)`, `partially guaranteed ($Y)` or a bare `$Y`.
   - **Teams.** Team is the nickname (`Jazz`, `Trail Blazers`), matched against `teams.py` with
     `Sixers` read as `76ers`.
   - **Result.** Each line yields a `GuaranteeLine(day, team_id, player, before, salary: int | None, after: int,
     settled: bool)`. A line that does not match the pattern raises, naming the line.
   - **Salary.** `salary` is the line's full-salary figure: `Non-guaranteed salary ($X)`, or the `full
     guarantee ($Y)` amount.
6. **Row per listed player.** A row is built for each `NonGuaranteedLine` whose player resolves.
   Its guarantee-date lines are those resolving to the same player and team; a line for the player
   under another team (an earlier deal) is ignored.
   - `salary`: the list's amount, else the full-salary figure of any of his guarantee-date lines,
     else `NULL`.
   - `minimum_salary`: true when the list gives no amount ("minimum-salary contracts unless otherwise
     noted").
   - `guaranteed`: the list's partial amount, else `0`.
   - `guarantee_date` / `guaranteed_after`: from his first **unsettled** guarantee-date line on or after
     the run date, the line's date and its after amount (fully guaranteed means `salary`). With no such
     line, January 7 of the season's second year and `salary`, which may be `NULL`. A player still
     under contract through January 7 receives his full salary: the league-wide default.
   - `season`, `as_of_date` (the run date), `source='hoopsrumors'`.
7. **Snapshot semantics.** Each run makes its `as_of_date`'s `hoopsrumors` rows exactly the list:
   insert, update when values changed, delete a player the list no longer has. This is
   `impact.replace_snapshot`'s pattern, on `contract`'s natural key
   `(player_id, team_id, season, as_of_date, source)`. Earlier weeks' snapshots are kept;
   NEU-1878 reads the latest `as_of_date` on or before its game date. A re-run the same day writes
   nothing.
8. **Names.** Names resolve through `resolve.name_index` and `resolve.rosters` (the team's roster
   on the run date) with `aliases.yml`, as DARKO's fallback does. An unresolved name is logged and has no
   row. The run's detail lists it twice: as `"Name (TRI)"` under `unresolved`, and as a
   ready-to-paste `aliases.yml` line, `"Name: <player ID>"`, under `aliases_to_add`. The alias
   file's header comment already documents that format. A player listed twice keeps his first line;
   the second is listed under `duplicates`.
9. **Schema migration on `contract`** (empty: nothing has written it).
   - **Change:** the `source` check becomes `IN ('hoopsrumors')`, and `salary` becomes nullable.
   - **Add:** `minimum_salary boolean NOT NULL`, `guarantee_date date NOT NULL` and `guaranteed_after integer NULL`.
   - **Drop:** `option_type` (and its check), `option_date`, `trade_eligible_date` and `two_way`.
   - **In `models.py`:** `CONTRACT_SOURCES = ("hoopsrumors",)`, and `OPTION_TYPES` goes. The natural key is unchanged.
10. **Run detail.** `enabled`, `files`, `updated`, `season`, `rows`, `changed`, `removed`,
    `unresolved`, `aliases_to_add`, `duplicates`, plus `ignored_lines`: guarantee-date lines for a
    player and team with no list row.

## Code shape

- `sources/hoopsrumors.py`: `ARTICLES`, `fetch(client, cache, day, season) -> tuple[Path, Path]`,
  `parse_non_guaranteed(path)` and `parse_guarantee_dates(path, season)`. The parser returns
  named headings unresolved (`day: date | Literal["league_opener", "team_opener"]`), so it stays pure.
  The module docstring records the layout and the 2026-10-23 terms finding, as `sources/darko.py` does.
- `contracts.py`: the overlay. It holds `ContractRow`, `build_rows(...)` (pure: lines, resolver,
  openers → rows plus the report lists), `replace_contracts(session, day, rows)` and `run(jobs, day)`.
- `overlays.py`: `register_overlay("contracts", "weekly", contracts.run)`.
- `settings.py`: `spotrac_enabled` → `hoopsrumors_enabled`, and `deploy/.env.example` likewise.
- `names.py` docstring: "Spotrac" → "Hoops Rumors".
- Fixtures: `backend/tests/fixtures/hoopsrumors/{non_guaranteed,guarantee_dates}/2026-10-23.html`,
  kept whole, plus a `README.md` with the hand-counted figures the tests assert, as
  `tests/fixtures/darko/README.md` does. On 2026-10-23 the list showed 22 players across 15 teams
  (15 teams `None`), updated 10-22-26. The guarantee dates had two unsettled lines: Mario Hezonja
  (Cavaliers) on November 15, $2,845,883 becoming fully guaranteed, and Trendon Watford
  (Pelicans) on December 30, partial $500,000 increasing to full $2,845,883. Recount from the
  recorded file before asserting.
- Tests in `tests/test_contracts.py`.

## Doc updates in the same PR

- **Project spec:**
  - §2 "Contracts" row: Hoops Rumors' two articles weekly; non-guaranteed contracts only; no fallback.
  - §3.1: `hoopsrumors.py` in place of `spotrac.py`, and `contracts.py`.
  - §3.2: "Hoops Rumors" in place of "Spotrac".
  - §3.3 `weekly`: "Hoops Rumors contracts".
  - §4.6: rewritten from this spec.
  - §5: the `raw/hoopsrumors/...html` keys.
  - §6.3 `contract`: the new columns.
  - §10: `HOOPSRUMORS_ENABLED`.
- **`CONTEXT.md`:** the **Non-guaranteed contract** term (added with this spec).
- **NEU-1878's description:** `contract_pressure` reads `guarantee_date` and `guaranteed_after`;
  `option_date` no longer exists.

## Acceptance criteria

- [ ] The Hoops Rumors terms and robots.txt finding is recorded as a NEU-1873 comment, beside the
      Spotrac/RealGM one.
- [ ] `parse_non_guaranteed` on the fixture yields every listed player (the README's count) under
      the right team, with `None` teams yielding none. Duncan Robinson (DET) reads salary
      15,992,957 and partial 2,000,000, Tolu Smith (DET) no salary and no partial, and `updated` is
      2026-10-22.
- [ ] `parse_guarantee_dates` on the fixture:
  - reads every line;
  - marks every ✅/❌ line `settled`;
  - leaves Hezonja (2026-11-15) and Watford (2026-12-30) unsettled;
  - returns the two named headings unresolved;
  - reads "increases to $500,000" (Bamba, Watford) as after 500,000.
- [ ] `build_rows` for run date 2026-10-23 gives the following, and every other row
      `guarantee_date` 2027-01-07:
      | Player | Salary | Minimum | Guaranteed | Guarantee date | After |
      |---|---|---|---|---|---|
      | Watford | 2,845,883 | true | 500,000 | 2026-12-30 | 2,845,883 |
      | Hezonja | 2,845,883 | true | 0 | 2026-11-15 | 2,845,883 |
      | Robinson | 15,992,957 | false | 2,000,000 | 2027-01-07 | 15,992,957 |
      | Tolu Smith | NULL | true | 0 | 2027-01-07 | NULL |
- [ ] A name the index cannot resolve has no row and appears under `unresolved` as `"Name (TRI)"`
      and under `aliases_to_add` in `aliases.yml`'s `Name: <player ID>` format.
- [ ] `HOOPSRUMORS_ENABLED=false` makes `run` return `{"enabled": False, "rows": 0}` with no
      request (the fake http client records none).
- [ ] A season missing from `ARTICLES` raises an error naming it. A page without the article title,
      or a list with fewer than 30 team headings, raises and caches nothing.
- [ ] A re-run on the same day reads the cached pages and reports `changed` 0 and `removed` 0.
      A player dropped from a later run's list is absent from that `as_of_date`'s rows, and his
      earlier row is kept.
- [ ] The Alembic migration upgrades and downgrades cleanly (`tests/test_migrations.py`), and
      `contract` has exactly the columns above.
- [ ] `contracts` is registered in the `weekly` family, and `weekly` runs it after the shot charts.

## Out of scope

- The `contract_pressure` flag itself (NEU-1878, M5).
- Salaries of fully guaranteed players, player and team options, and trade-eligible dates: no
  permitted source carries them. A later source adds its own columns.
- Two-way contracts and the two-way day limit: those come from `roster_membership` and `player_game`
  (NEU-1878).
- Discovering next season's article URLs automatically.

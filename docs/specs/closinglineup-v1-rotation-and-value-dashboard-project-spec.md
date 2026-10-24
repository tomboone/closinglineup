# closinglineup v1: rotation & value dashboard — project spec

Linear initiative: closinglineup (I-13). Project: P-NEU-101. Team: NEU. Glossary: `CONTEXT.md` at the repo root. Decisions and their rationale: `docs/adr/`. Original brief: `docs/handoff/nba-rotation-dashboard-handoff.md`.
This spec is the shared reference for every ticket in the project. A ticket needs its own `/planit` spec only where this document is silent.

Reference implementation: nba-today (`~/projects/nba-today`, tomboone/nba-today) for the curl_cffi NBA.com session, settings, JSON logging, clock, heartbeat, scheduler loop and the kumadrome deploy README. Where this spec says **"port from nba-today"**, copy the module, rename the package, and change only what this spec calls for. Do not extract a shared package.

## 1. Purpose

Build closinglineup, from an empty repo, into a private single-user NBA dashboard that runs entirely on the home Mac mini `kumadrome` (Ubuntu, Docker, residential IP) and is reached over the personal tailnet. It answers, every morning, whose minutes are moving and why: each player's **rotation state** on his team on each game date, the **rotation events** between dates, and the minutes, production and impact trends behind them, over 5/10/20-game, season and prior-season **windows**. Minutes lead; box production lags; every view shows the minutes trend first.

Non-goals for v1: fantasy, any ML or predictive model, own RAPM/EPM, play-type classification, betting, mobile-first UI, auth, sharing, WNBA/G League as first-class data (keep IDs compatible), playoff-specific handling, Basketball Reference as a source.

## 2. Decisions (summary; see ADRs for rationale)

| Area | Decision |
|---|---|
| Serving tier | Everything on kumadrome in one compose project; the UI is reached through a userspace Tailscale sidecar on the personal tailnet (ADR-0001). No Coolify, no public surface. |
| NBA.com access | One curl_cffi client (port from nba-today's `nba_http.py`), ~1 req/s with jitter, retries. `nba_api` is not a dependency. pbpstats runs in file mode over our raw cache and never makes a network call (ADR-0002). |
| Store | Postgres 17. Raw JSON cache on disk → parquet per table per date → `COPY` into Postgres. pg_dump for backups. |
| Derived layer | `player_window_stats` is a materialized view refreshed after each nightly load. `rotation_state` and `rotation_event` are tables written by an idempotent `derive` step (ADR-0003). |
| Rotation tiers | Injured > Starter > Core > Fringe > Out, in that precedence. Two-way, 10-day, Exhibit 10 and G League assignment are badges, not tiers. |
| Windows | Role windows (minutes, starter/closer rate, rank, tier, flags) are per player-team. Production windows (efficiency, usage, shots, impact) and season baselines are per player. Short windows are shown as short, never padded. |
| Garbage time | Every stint carries `is_garbage_time`. Reconciliation against `leaguedashlineups` uses unfiltered sums; the UI defaults to filtered with a toggle. Thresholds are one config object (§7.4). |
| Injuries | Official NBA injury-report PDF, parsed several times a day, is primary. ESPN's injuries JSON (nba-today client, plain httpx) is the fallback and is marked stale in the UI when used. |
| Transactions | NBA.com player-movement JSON (`stats.nba.com/js/data/playermovement/NBA_Player_Movement.json`), daily. |
| Impact metrics | Local BPM always; EPM (Dunks & Threes) and DARKO daily snapshots, each behind a terms-of-use check at build time and an enable flag. `impact_snapshot.source` keeps them interchangeable. |
| Contracts | Hoops Rumors' two seasonal articles weekly, the non-guaranteed list and the early guarantee dates; non-guaranteed contracts only, no fallback. Spotrac's and RealGM's terms forbid scraping (ADR-0004). |
| Backfill | 2025-26 complete, 2026-27 to date. Shot charts weekly. |
| Scheduling | One long-running `worker` container with APScheduler. Every job is also a CLI command. |
| Frontend | React + Vite + TypeScript + ECharts, built into the app image and served as static files by FastAPI. Views: team, player, league morning page, game. |
| Repo | One public MIT repo `tomboone/closinglineup` with `backend/` and `frontend/`. |
| Toolchain | Backend: Python 3.13, uv, ruff, pyright strict, pytest, SQLAlchemy 2, Alembic, pydantic v2 + pydantic-settings, typer, APScheduler, curl_cffi, httpx, pbpstats, pyarrow, pdfplumber. Frontend: pnpm, Vite, React, TypeScript, React Router, TanStack Query, ECharts, oxlint, vitest, Node 22. |
| Local dev | Docker-only via compose; pre-commit hooks run inside containers (`docker compose exec -T`). |
| Build and deploy | GitHub Actions lints and tests every PR; on push to `main` it builds `ghcr.io/tomboone/closinglineup` (`:latest`, `:sha-<7>`). A Watchtower scoped to closinglineup pulls it on kumadrome. |
| Observability | Structured JSON logs to stdout. One healthchecks.io check per job family (nightly, daily, weekly), `/fail` on error. No Sentry, SigNoz or Bugsink in v1. |
| Counted games | Regular season (including NBA Cup group games), NBA Cup knockouts, play-in, playoffs. Never preseason, All-Star or Summer League. |
| Branch flow | Greenfield: every ticket branches from and PRs into `main`. No release branch. |

## 3. Architecture

### 3.1 Repo layout

```
backend/
  pyproject.toml  uv.lock
  src/closinglineup/
    __init__.py  __main__.py
    cli.py                 # typer: fetch | parse | load | derive | overlays | api | worker | health | reparse
    settings.py            # pydantic-settings; all env in one place                     (port)
    logging.py             # JSON log formatter                                           (port)
    clock.py               # Clock protocol, SystemClock, FakeClock                        (port)
    heartbeat.py           # healthchecks.io client, one URL per job family                (port, adapt)
    scheduler.py           # APScheduler jobs → CLI command functions
    season.py              # season label "2026-27", phase, counted-game filter
    teams.py               # canonical 30-team table: nba_team_id ↔ tricode ↔ espn id
    names.py               # player names → NBA.com IDs, normalized, plus aliases.yml
    aliases.yml            # source spellings that differ from NBA.com's, → player ID
    overlays.py            # overlay registry: name → job family + run function
    injuries.py            # the `injuries` overlay: injury report → injury_status
    contracts.py           # the `contracts` overlay: Hoops Rumors → contract
    sources/
      nba_http.py          # curl_cffi NbaSession (port). The only importer of curl_cffi.
      cache.py             # RawCache: path layout, write-once, read, list, integrity
      nba_stats.py         # stats.nba.com endpoint functions, one per endpoint
      nba_cdn.py           # cdn.nba.com schedule, live scoreboard, live box, live pbp
      injury_pdf.py        # official injury report: URL discovery + pdfplumber parse
      espn_injuries.py     # fallback (port nba-today's ESPN injuries client)
      transactions.py      # NBA.com player-movement JSON
      epm.py  darko.py     # impact metric scrapers
      hoopsrumors.py       # contracts: non-guaranteed list + guarantee dates
    parse/
      box.py  matchups.py  hustle.py  usage.py   # raw JSON → typed rows
      pbp.py               # pbpstats bridge: file-mode loaders → stints, possessions, lineups
      shots.py  roster.py  schedule.py
    load/
      parquet.py           # rows → parquet files under DATA_DIR/parquet/<table>/<date>.parquet
      loader.py            # parquet → staging → upsert into Postgres; idempotent per (table, date)
    derive/
      garbage_time.py      # the rule, as one config object + one function
      windows.py           # refresh materialized views
      rotation.py          # tier rules, rotation_state rows
      events.py            # rotation_event rows + contributing signals
      flags.py             # §7.7
      bpm.py               # local BPM
      reconcile.py         # vs leaguedashlineups / leagueplayerondetails
      run.py               # `derive`: every step over a run of dates, each in load_log
    db/
      models.py  session.py  views.sql
      alembic/             # migrations
    api/
      app.py               # FastAPI app, static mount for the built frontend
      routers/ league.py teams.py players.py games.py meta.py
      schemas.py           # response models; the frontend's TS types are generated from these
  tests/
    fixtures/              # recorded raw responses per endpoint, trimmed; injury/report/ PDFs
frontend/
  package.json  vite.config.ts  src/ ...
deploy/
  docker-compose.yml       # kumadrome: postgres, app, worker, tailscale sidecar, watchtower
  README.md
docker-compose.yml         # local dev: postgres, app (reload), worker, frontend (vite dev)
Dockerfile                 # multi-stage: node build of frontend/ → uv install of backend/ → runtime
.github/workflows/         # ci.yml (path-filtered backend/frontend jobs), image.yml
```

Rules:
- `sources/` is the only package that does network I/O. `parse/`, `derive/`, `api/schemas.py` are pure and fixture-tested.
- `nba_http.py` is the only importer of curl_cffi; `injury_pdf.py` the only importer of pdfplumber; `parse/pbp.py` the only importer of pbpstats.
- Every CLI command takes `--date` / `--from --to` and `--game-id` where it makes sense, and is safe to re-run.

### 3.2 Data flow

```
fetch  : NBA.com ──curl_cffi──▶ DATA_DIR/raw/<source>/<endpoint>/<key>.json   (write-once)
parse  : raw JSON ──▶ typed rows ──▶ DATA_DIR/parquet/<table>/<game_date>.parquet
         (pbpstats reads DATA_DIR/pbpstats/… in file mode for stints/possessions;
          shot files are per shot chart: DATA_DIR/parquet/shot/<player_id>_<season>_<type>.parquet)
load   : parquet ──COPY──▶ staging ──upsert──▶ Postgres tables (§6)
overlay: injury PDF / ESPN / transactions / EPM / DARKO / Hoops Rumors ──▶ snapshot tables (§6.3)
derive : refresh views → rotation_state → rotation_event → flags → bpm → reconcile
api    : FastAPI reads Postgres only
```

A `load_log` row per (table, game_date, step) records what ran; `fetch --missing` and `load --missing` use it to self-heal skipped dates on the next run. `derive` (`derive/run.py`) records each of its steps there too, as (`stint`, `garbage_time`), (`player_window_stats`, `windows`), (`rotation_state`, `tiers`), (`rotation_event`, `events`), (`player_flag`, `flags`), (`impact_snapshot`, `bpm`) and (`stint`, `reconcile`); it refreshes the view once per run, after every date's garbage time, and reconciles once, for the run's last date. A failed step stops the run, since a date's events read the previous game date's rotation state; `derive --missing`, and `nightly` for its season's earlier dates, derive again, oldest first, every date whose loads all succeeded and whose writing steps have not (NEU-1879).

### 3.3 Nightly and periodic jobs (America/New_York)

| Job | When | Steps | Heartbeat |
|---|---|---|---|
| `nightly` | 03:30 daily | schedule refresh → fetch yesterday's final games (all per-game endpoints) → parse → load → derive → reconcile sample | `HEALTHCHECKS_URL_NIGHTLY` |
| `injuries` | 10:00, 13:00, 16:00, 18:30 | injury PDF (ESPN fallback) → `injury_status` | `HEALTHCHECKS_URL_DAILY` (last run of the day pings) |
| `daily_overlays` | 09:00 | transactions, EPM, DARKO, rosters (`commonteamroster`, `commonallplayers`) | `HEALTHCHECKS_URL_DAILY` |
| `weekly` | Mon 04:30 | shot charts for every active player, Hoops Rumors contracts, `leaguedash*` cross-check pulls | `HEALTHCHECKS_URL_WEEKLY` |
| `poll_finals` | every 30 min 18:00–02:00 | cdn.nba.com scoreboard: mark games final so `nightly` knows what to fetch | none |

A job that fails pings `/fail` with the exception summary in the body and leaves `load_log` rows for the next run to pick up. Jobs never overlap: APScheduler `max_instances=1`, `coalesce=True`.

## 4. Sources

All requests to NBA.com go through `NbaSession` (curl_cffi, `impersonate="chrome"`, `Origin: https://www.nba.com`, `Referer: https://www.nba.com/`, `Accept: application/json`), rate-limited to `NBA_RPS` (default 1.0) with ±30% jitter, 3 retries with exponential backoff on connection errors and 5xx. Responses are written to the raw cache before parsing; a cached key is never re-fetched unless `--force`.

### 4.1 stats.nba.com (`sources/nba_stats.py`)

One function per endpoint; parameter names follow nba_api's documentation for the same endpoint. Required parameters that nba_api defaults are passed explicitly.

| Endpoint | Key params | Used for | Cadence |
|---|---|---|---|
| `scoreboardv3` | `GameDate`, `LeagueID=00` | game status for a date | nightly, backfill |
| `scheduleleaguev2` | `LeagueID=00`, `Season` | any season's schedule, in cdn.nba.com's shape; the CDN serves only the current season's (NEU-1864) | backfill |
| `boxscoretraditionalv3` | `GameID`, `StartPeriod=0`, `EndPeriod=14`, `StartRange=0`, `EndRange=0`, `RangeType=0` | starters, minutes, traditional box, DNP reasons | per game |
| `boxscoreadvancedv3` | same | TS%, eFG%, usage, AST%, TOV%, REB%, ratings | per game |
| `boxscorehustlev2` | `GameID` | deflections, contested shots, charges | per game |
| `boxscorematchupsv3` | `GameID` | matchup rows | per game |
| `boxscoreusagev3` | same as `boxscoretraditionalv3` | usage split | per game |
| `playbyplayv3` | `GameID`, `StartPeriod=0`, `EndPeriod=14` | our own PBP (shot clock, descriptions) | per game |
| `boxscoretraditionalv2` | `GameID`, `StartPeriod=0`, `EndPeriod=14`, `StartRange=0`, `EndRange=55800`, `RangeType=2` (pbpstats's own range; `RangeType=0` returns no rows) | the v2 resultSets box. pbpstats's `live` loaders, which `parse/pbp.py` uses, do not read it (NEU-1857) | per game |
| `playbyplayv2` | `GameID`, `StartPeriod=0`, `EndPeriod=14` | nothing: it returns `{}` for every game as of 2026-10-21 (NEU-1855), so the fetcher reports it failed, and pbpstats reads cdn.nba.com's live play-by-play instead (§4.2, NEU-1857) | per game |
| `shotchartdetail` | `PlayerID`, `TeamID=0`, `Season`, `SeasonType`, `ContextMeasure=FGA`, `LeagueID=00`, plus the empty-string params nba_api sends | shot rows | weekly per active player |
| `commonteamroster` | `TeamID`, `Season` | roster, contract type hints | daily |
| `commonallplayers` | `LeagueID=00`, `Season`, `IsOnlyCurrentSeason=0` | player reference | daily |
| `playergamelogs` | `Season`, `SeasonType` | backfill cross-check | backfill |
| `leaguedashlineups` | `Season`, `SeasonType`, `MeasureType=Advanced`, `GroupQuantity=5`, `PerMode=Totals` | reconciliation | weekly; re-fetched nightly for the reconcile sample, so it covers the same games as the stints (NEU-1865) |
| `leagueplayerondetails` | `TeamID`, `Season`, `SeasonType`, `MeasureType=Advanced` | reconciliation of on-court ratings: it lists only On rows (NEU-1865) | weekly; nightly for the reconcile sample's two teams |
| `leaguedashplayerstats`, `leaguedashteamstats` | `Season`, `SeasonType`, `MeasureType` | cross-checks | weekly |

pbpstats runs its `live` loaders in file mode over cdn.nba.com's live play-by-play (§4.2), since its `stats_nba` loaders need `playbyplayv2`. The exact layout (`pbp/live_<game_id>.json` and `overrides/missing_period_starters.json`) was verified against the pinned pbpstats 1.3.11 in NEU-1857 and is recorded in `parse/pbp.py`'s docstring. `parse.pbp.materialize` writes the file from the raw cache.

### 4.2 cdn.nba.com (`sources/nba_cdn.py`)

- `static/json/staticData/scheduleLeagueV2_1.json` — the season schedule; the source of `game` rows and the counted-game filter.
- `static/json/liveData/scoreboard/todaysScoreboard_00.json` — same-day final polling.
- `static/json/liveData/boxscore/boxscore_<game_id>.json`, `static/json/liveData/playbyplay/playbyplay_<game_id>.json` — fallback box and PBP when stats.nba.com fails for a game; parsed into the same row types with `source='cdn'`. The live play-by-play is also the input pbpstats reads for stints and possessions, for every game (§4.1, NEU-1857), so it needs no stats.nba.com counterpart. A game whose stats.nba.com endpoints (`playbyplayv2` aside) are not all cached after `nightly`'s fetch is `stats_failed`: the date's (`player_game`, `fetch_stats`) `load_log` row lists it, failed, and the run succeeds with a warning. Once stats.nba.com stops answering (`NbaUnreachableError`), the run asks it nothing more: the rest of the date's games come from cdn.nba.com alone, and reconciliation is skipped. Without its traditional box, its `player_game` rows come from the live box: its active players only, as stats.nba.com lists them, with the counting stats and NULL rates, ratings and hustle numbers. `fetch --missing` re-tries stats.nba.com for every `stats_failed` game, then parses and loads their dates again, the stats.nba.com rows replacing the game's cdn rows (NEU-1890).

The same `NbaSession` is used; cdn.nba.com also fingerprints TLS.

### 4.3 Injury report (`sources/injury_pdf.py`)

The NBA publishes `https://ak-static.cms.nba.com/referee/injury/Injury-Report_<YYYY-MM-DD>_<HH>_<MM>[AM|PM].pdf` every 15 minutes around the clock on any day with games that day or the next (none in the All-Star break), stamped in Eastern time; a report not published answers 403 (pinned in NEU-1867; the stamp had no minutes, and reports were hourly, until partway through 2025-26). Discovery tries the newest quarter hours of the date first. Parse with pdfplumber into rows `(report_datetime, game_date, game_id?, team, player, status, reason)`, status ∈ {Out, Doubtful, Questionable, Probable, Available}. Games are matched to `game` rows on date + matchup text. The parser is isolated and fixture-tested against at least three real PDFs (a weekday, a weekend, one with "NOT YET SUBMITTED" teams). On fetch or parse failure, or a report with no lines on a date with games, the ESPN injuries client runs and its rows are stored with `source='espn'`, no `game_id` and the run's date as `game_date`, and `status` mapped (Out→Out, Day-To-Day→Questionable, GTD→Questionable), a player going to the team ESPN gives for him over the team it lists him under; the UI shows a stale marker when the latest row for a team is from ESPN.

### 4.4 Transactions (`sources/transactions.py`)

`NBA_Player_Movement.json` rows → `transaction(date, type, player, from_team, to_team, detail)` with `type` classified from the description: sign, waive, trade, two_way_sign, two_way_convert, assign, recall, option_exercise, option_decline, other. The feed is every move since 2015-07-01, so the overlay makes the table hold exactly its rows; as of 2026-10-23 it holds only signings, claims, waivers, trades and contract conversions, and no G League assignment, recall or option decision (NEU-1869). A G League assignment is therefore read from the official injury report (§4.3), whose `G League - On Assignment` lines `derive.roster.current_g_league_assignment` reads, a recall being absence from a newer report that covers the team; an `assign`/`recall` row, should the feed ever carry one, decides from its date on (NEU-1905).

### 4.5 Impact metrics (`sources/epm.py`, `sources/darko.py`, `derive/bpm.py`)

- EPM: Dunks & Threes player table, daily, `source='epm'`, with offensive and defensive EPM and minutes. Terms of use checked and recorded in the ticket before scheduling; `EPM_ENABLED` flag.
- DARKO: darko.app's DPM leaderboard, daily, `source='darko'`, DPM with O/D split and projected minutes (`x_minutes`) as `minutes_basis`. Its "Download CSV" button serializes the board in the browser, so the fetch is the data file behind it, the leaderboard route's SvelteKit `/__data.json` (devalue-serialized; `sources/darko.py` documents the layout), which carries every player's `nba_id`; a line without one resolves by name and the alias table (NEU-1871). The Shiny app at apanalytics.shinyapps.io was taken offline in June 2026 and points to darko.app, which publishes no terms of use and no robots.txt (checked 2026-10-23). `DARKO_ENABLED` flag; off, the overlay is a no-op with no request.
- BPM: Basketball Reference's published BPM 2.0 formula over `player_game` sums and team ratings, computed in `derive`, `source='bpm_local'`, always on. `derive/bpm.py` runs as a `nightly` step after load, over the season's regular-season games final through the date, with the team adjustment to the team's own net rating (not schedule-adjusted) and the lead correction from half its margin; every 2025-26 player with 1,000+ minutes lands within ±1.0 of the published value, and the league's minutes-weighted mean within ±0.2 of 0 (`tests/test_bpm.py`, NEU-1872).

### 4.6 Contracts (`sources/hoopsrumors.py`, `contracts.py`)

Spotrac's and RealGM's terms forbid automated access, and both answer a non-browser client with 403; ESPN's terms forbid scraping, and HoopsHype is Spotrac's (ADR-0004). Hoops Rumors' robots.txt allows its articles (`Crawl-delay: 1`) and it publishes no terms of use. Each season, until January 10, it maintains two articles, whose URLs are committed per season in `sources/hoopsrumors.py`'s `ARTICLES` (added by hand, May to July). A season with no entry raises, naming it.

- **"<season> Non-Guaranteed Contracts By Team"**: an `<h3>` per team, by full name, over its players without a fully guaranteed salary (non-guaranteed, partially guaranteed, Exhibit 9 or 10): `Name`, or `Name ($X)` for one not on a minimum salary, optionally with `Partially guaranteed for $Y.` Two-way contracts are not on it.
- **"Early NBA Salary Guarantee Dates For <season>"**: lines under date headings (June to December in the season's first year, January to May in its second), or under "First day of NBA regular season" or "First day of team's regular season". Each line is `Name (Team): <before> becomes|increases to <after>.`, ending `(✅)` or `(❌)` once settled.

The `contracts` overlay (weekly, behind `HOOPSRUMORS_ENABLED`) fetches both pages with plain httpx, a second apart. It makes the run date's `source='hoopsrumors'` rows exactly the list: one per listed player whose name resolves. The name resolves through the name index and `aliases.yml` against the team's roster on the date, as in §4.5's fallback.

Each row has:

- **salary**: the list's amount, else his guarantee-date lines' full salary, else NULL, with `minimum_salary` set when the list gives no amount;
- **guaranteed**: the partial guarantee, else 0;
- **guarantee_date** and **guaranteed_after**: from his first unsettled guarantee-date line on or after the run date for the same team. A named heading resolves to the season's, or the team's, first regular-season game. With no such line, they are January 7 and his salary.

A player absent from the list is taken as fully guaranteed. Two-way status is `roster_membership`'s alone. An unresolved name has no row and is listed in the run's detail as `Name (TRI)`, with its ready-to-paste `aliases.yml` line, `Name: <player ID>`. A fetch or parse failure fails the step and leaves the last snapshot in place. No source carries options, trade-eligible dates or fully guaranteed salaries (NEU-1873).

## 5. Raw cache (`sources/cache.py`)

`DATA_DIR/raw/<source>/<endpoint>/<key>.json` where `key` is the game ID, the date, `<player_id>_<season>` or `<team_id>_<season>` as appropriate; injury PDFs are kept as `raw/injury/report/<report_datetime>.pdf`, the report's Eastern time as `YYYY-MM-DDTHHMM` (NEU-1867). ESPN's injuries, a snapshot fetched afresh on each fallback, are kept as `raw/espn/injuries/<instant fetched, UTC as YYYYMMDDTHHMMSSZ>.json`, like today's CDN scoreboard (NEU-1868). darko.app's leaderboard is kept as `raw/darko/leaderboard/<date>.json`, the date it is fetched for, so the day's re-run reads it (NEU-1871). Hoops Rumors' two articles are kept the same way, as `raw/hoopsrumors/non_guaranteed/<date>.html` and `raw/hoopsrumors/guarantee_dates/<date>.html` (NEU-1873). Files are write-once; a `.meta.json` sibling records `fetched_at`, status and the request params. `closinglineup reparse --from --to` re-runs parse → load → derive from the cache without the network. pbpstats override files (`DATA_DIR/pbpstats/overrides/`) are committed to the repo under `backend/pbpstats_overrides/` and copied in by `parse.pbp.materialize` before pbpstats reads a game.

## 6. Data model (Postgres, schema `public`)

Every table has `created_at` / `updated_at`. IDs are NBA's (`nba_player_id`, `nba_team_id`, `game_id` as text, e.g. `0022500001`). Natural keys are unique constraints; loads upsert on them.

### 6.1 Reference

- `team(nba_team_id PK, tricode, name, conference, division)`
- `player(nba_player_id PK, name, birthdate, draft_year, draft_pick, height_in, position)`
- `roster_membership(id, player_id, team_id, start_date, end_date NULL, contract_type ∈ {standard, two_way, ten_day, exhibit_10}, source)` — rebuilt whole nightly by `derive.roster` from every cached roster, transaction and `player_game` row; a player is on one team per date. A span opens at the first evidence of him on a team and runs until a waiver (which ends it that day), a trade (the day before), or evidence of him elsewhere or a later roster without him (which end it at the last evidence of him on the team, his last game, say); `source` is what opened it (`transaction`, `player_game`, `commonteamroster`) (NEU-1864, NEU-1869).
- `game(game_id PK, season, game_date, phase ∈ {regular, cup_knockout, play_in, playoff}, home_team_id, away_team_id, status ∈ {scheduled, final, postponed}, home_score, away_score, home_b2b, away_b2b)`

### 6.2 Per game

- `player_game(game_id, player_id, team_id, is_starter, seconds, dnp_reason NULL ∈ {cd, injury, inactive, suspension, g_league, other}, pts, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, ast, stl, blk, tov, pf, plus_minus, ts_pct, efg_pct, usg_pct, ast_pct, tov_pct, oreb_pct, dreb_pct, off_rating, def_rating, pace, deflections, contested_shots, charges_drawn, source ∈ {stats, cdn})` PK `(game_id, player_id)`
- `stint(id, game_id, period, start_sec, end_sec, team_id, lineup_key, opp_lineup_key, possessions, pts_for, pts_against, is_garbage_time)` — one row per team per stint; `lineup_key` = five sorted player IDs joined by `-`.
- `possession(id, game_id, …pbpstats columns as emitted…)` — pbpstats's schema plus `game_id`.
- `possession_lineup(id, game_id, period, number, team_id, lineup_key)` — each five NBA.com credits with a possession, on offense or defense; read only by reconciliation (§11, NEU-1900).
- `shot(id, game_id, player_id, team_id, period, clock_sec, x, y, distance_ft, zone ∈ {rim, short_mid, long_mid, corner_3, above_break_3}, shot_type, made, assisted_by NULL, shot_clock_bucket NULL)` — keyed by `(game_id, event_num)`, NBA.com's `GAME_EVENT_ID`; a load of a player's shot chart replaces his rows for that season and season type (NEU-1863)
- `matchup(game_id, off_player_id, def_player_id, partial_poss, pts, fgm, fga, fg3m, fg3a, tov, ast)` PK `(game_id, off_player_id, def_player_id)`

### 6.3 Snapshots

- `injury_status(id, report_datetime, game_date, game_id NULL, player_id, team_id, status, reason, source ∈ {pdf, espn})`
- `transaction(id, date, type, player_id, from_team_id NULL, to_team_id NULL, detail)`
- `impact_snapshot(date, player_id, source ∈ {epm, darko, bpm_local}, value, o_value, d_value, minutes_basis)` PK `(date, player_id, source)`
- `contract(id, player_id, team_id, season, salary NULL, minimum_salary, guaranteed, guarantee_date, guaranteed_after NULL, as_of_date, source ∈ {hoopsrumors})` — non-guaranteed contracts only, one row per listed player per weekly `as_of_date`; a player with no row that week is fully guaranteed (ADR-0004). `salary` and `guaranteed_after` are NULL for a minimum-salary contract no source line prices. `team_id` is in the natural key, `(player_id, team_id, season, as_of_date, source)`: a waived player's old contract and his new team's are each a row (NEU-1866, NEU-1873)

### 6.4 Derived

- `player_window_stats` — **materialized view**, one row per `(player_id, team_id NULL for production windows, game_date, window ∈ {5, 10, 20, season, prior_season})` with every §7 metric plus `games_in_window` and `is_short`. Role rows (team set) are windows 5, 10, 20 and season over the player's spell with the team, his roster spans there run together; production rows (team NULL) add prior_season. Every window but prior_season stays within the date's season. Each windowed metric has `<metric>_delta_prior` and `<metric>_delta_season` columns; a production row's season window has the prior season as its prior, a role row's has none. §7.3's 2-man combos and garbage-time-included ratings are not in it yet. Defined in `db/views.sql`, refreshed by `derive.windows.refresh_windows` (NEU-1875).
- `rotation_state(player_id, team_id, game_date, tier, minutes_share_10, starter_rate_5, played_rate_10, availability, badges text[], is_short, inputs jsonb)` PK `(player_id, team_id, game_date)`
- `rotation_event(id, player_id, team_id, game_date, prev_game_date NULL, from_tier NULL, to_tier, signals jsonb)` — `from_tier NULL` means arrival on the team; one per `(player_id, team_id, game_date)` (NEU-1877).
- `player_flag(player_id, team_id, game_date, flag, detail jsonb)` PK `(player_id, team_id, game_date, flag)` — `detail` carries the numbers that raised the flag (NEU-1878).
- `load_log(table_name, game_date, step, status, started_at, finished_at, detail)` and `job_run(job, started_at, finished_at, status, detail)`.

## 7. Metrics and rules

All window metrics carry `delta_prior` (vs the equal-length window immediately before) and `delta_season` (vs the season window). Windows are keyed by the team's game dates; a window at date d includes d.

### 7.1 Minutes and role (role windows)

- minutes per game played; minutes share = Σ player seconds ÷ Σ team available player-seconds (48 × 5 minutes per game plus overtime) over team games in the window
- minutes volatility = stdev of minutes over games played in the window
- starter rate = games started ÷ games played; closer rate = games on the floor in the final 5:00 within 10 points ÷ team games in which such a situation occurred
- rotation rank = ordinal by minutes share within team for the window
- dnp_cd_count, inactive_count

### 7.2 Efficiency and production (production windows)

TS%, eFG%, USG%, AST%, TOV%, ORB%, DRB%, FTr, 3PAr; shot profile frequency and FG% by zone from `shot`; PTS/AST/REB per 36 and per 100 possessions.

### 7.3 On-court impact (role windows, garbage-time filtered by default)

ORtg/DRtg/NetRtg on court from stints; on/off net rating with possession counts; net rating of the player's three most-used 2-man combos; latest `impact_snapshot` per source with O/D split.

### 7.4 Garbage time

A stint is garbage time when, at its start, `|margin| ≥ M` with `≤ T` seconds left in the fourth quarter for any `(M, T)` in `GARBAGE_TIME_RULE = [(25, 540), (20, 360), (15, 180), (10, 60)]`, and no later stint in the game is within 10. One config object, one function, unit-tested.

### 7.5 Rotation tiers (per player-team-game-date, role window)

Evaluated in order; the first match wins:
1. **Injured** — the most recent injury report at or before the date lists the player Out or Doubtful for that team's game on that date, for a reason other than a G League one (`G League - On Assignment`, `G League - Two-Way`), which is an assignment, not an injury (`derive/availability.py`, NEU-1876).
2. **Starter** — started ≥ 4 of the last 5 games he played for the team.
3. **Core** — minutes share ≥ 3.6% over the last 10 team games: 18% of a game's 48 minutes, about 8.6 a game, since §7.1's share is of the team's 240 player-minutes a regulation game, of which a whole game is 20% (NEU-1876).
4. **Fringe** — played in ≥ 50% of the last 10 team games.
5. **Out** — everything else, including a current G League assignment.

Thresholds live in `TierRule` (one settings object). A role window shorter than its nominal length computes on what it has and sets `is_short`. Badges: `two_way`, `ten_day`, `exhibit_10` from `roster_membership.contract_type`; `g_league` from the official injury report's `G League - On Assignment` lines, whatever their status, held until a newer report covering the team omits the player (§4.4, NEU-1905).

### 7.6 Rotation events and signals

An event is written whenever the tier at date d differs from the tier at the team's previous game date, or when the player-team has no previous row. `signals` lists the top three of: Δ 5-game vs 20-game minutes share, minutes volatility, starter-rate change, injury status change in the last 7 days, transaction in the last 7 days, on-court net change (10g), usage change (10g), each with its value and a plain-English label, ranked by magnitude relative to the player's own 20-game standard deviation. A numeric signal's magnitude is its change over the standard deviation, across his 20-game role window, of the per-game quantity it measures (his per-game minutes share, minutes, whether he started, on-court net; usage, both its change and its scale, over his 20-game production window instead), so a nonzero change always has a nonzero scale, and none exceeds about 2 sd, a whole step change. The injury and transaction signals, each over the last 7 days, the date included, carry fixed magnitudes within that range (`SignalRule`, 1.5 and 1.0), so an injury return outranks a modest minutes change but not a cliff. Each signal is stored as `{name, value, label}`; a date's events are deleted and rewritten on every run (`derive/events.py`, NEU-1877).

### 7.7 Flags (per player-team-game-date)

- `minutes_cliff` — 5-game minutes share < 60% of 20-game share, not Injured
- `usage_up_efficiency_down` — USG% +3 pts and TS% −4 pts, 10-game vs season
- `bleeding` — on-court net ≤ −5 over 10 games with ≥ 200 possessions while team on/off without him is positive
- `earning_trust` — minutes share up across three consecutive 5-game windows
- `yo_yo` — ≥ 3 rotation events in the last 20 team games
- `contract_pressure` — non-guaranteed money with a guarantee date within 30 days, or a two-way player within 5 of the NBA-day limit

Every player-team with a `rotation_state` row on the date is evaluated; thresholds live in `FlagRule`. "At least", "at most" and "within" are inclusive, the cliff's "below" strict, and values within 1e-9 count as equal. A NULL metric raises nothing. `usage_up_efficiency_down` reads the 10-game production window's `_delta_season` columns; `bleeding` the 10-game role window's `on_court_net` and `on_court_poss`, the team's net with him off being `on_court_net − on_off_net`. `earning_trust` compares the 5-game role window at the date and 5 and 10 team games before it, each strictly higher than the last, and needs 15 games of his current spell this season. `yo_yo` counts rotation events, arrivals among them, in the team's last 20 final games this season, the date included. `contract_pressure` reads the latest weekly `contract` snapshot on or before the date (a player it omits is fully guaranteed): his next `guarantee_date` 0 to 30 days ahead whose `guaranteed_after` exceeds `guaranteed`, or is NULL; or a two-way span covering the date with at least 45 of the CBA's 50 regular-season games active (NBA Cup knockouts included; a box-score line he played in or sat out as a coach's decision) since it started, this season, the limit not prorated for a mid-season signing. A date's flags are deleted and rewritten on every run (`derive/flags.py`, NEU-1878).

## 8. API (`/api`)

JSON only; response models in `api/schemas.py`; the frontend's `src/api/types.ts` is generated from the OpenAPI schema in CI and committed. All dates are `YYYY-MM-DD` game dates; `?date=` defaults to the latest derived date. `?garbage_time=include|exclude` defaults to exclude.

| Route | Returns |
|---|---|
| `GET /api/meta` | latest derived date, last job runs, freshness per source, stale markers |
| `GET /api/league?date=` | tier transitions grouped by team, top/bottom 15 10-game minutes-share movers, flags raised, injury changes, transactions, top/bottom 10 by 10-game impact delta |
| `GET /api/teams` | the 30 teams with record and latest derived date |
| `GET /api/teams/{tricode}?date=` | tier board (players with tier, badges, sparkline series for minutes share, on/off net, impact), last 5 closing lineups, top 10 5-man lineups and top 10 2-man combos by possessions with net rating and 10-game trend, availability board, roster counts vs limits, contract notes |
| `GET /api/players/{id}?window=&date=` | header (team, tier, badges), time series per window for minutes & share, on/off net, TS% & USG%, impact, with annotations (injury statuses, transactions, starter/closer changes); shot profile for window vs season vs prior season; top shared-floor teammates with net; matchup summary; flag history |
| `GET /api/games/{game_id}` | both teams' stints as a timeline, lineup results, closing lineup, garbage-time marker |
| `GET /api/health` | 200 when the DB answers and the latest derived date is within 36 h of the last final game |

The OpenAPI schema is served at `/api/openapi.json` and snapshot-tested (`tests/fixtures/api/openapi.json`, rewritten by `UPDATE_SNAPSHOTS=1`). Every route above is registered with its response model, so the schema carries them all. The latest derived date is the latest game date every writing `derive` step has succeeded for in `load_log`, as `derive.run` counts one derived (§3.2); `?date=` with none derived yet is a 404. The team page's sparklines read `player_window_stats`' 10-game windows over the team's last 20 game dates, so their ratings are garbage-time filtered whatever `?garbage_time=` says; its lineups and 2-man combos are summed from stints, and follow it. Its contract notes are the non-guaranteed rows only, since no source carries options or trade-eligible dates (§4.6) (NEU-1880). The player page's `?window=` picks its series' points, a point per game: the latest 5, 10 or 20 of the player's team games this season through the date, or all of them, each that game's own value rather than a window's; annotations, teammates and matchups keep to the same games, its on/off series and teammates are summed from stints and follow `?garbage_time=`, its shot profile is `player_window_stats`' production rows, its flag history is the season's, and an unknown player ID is a 404 (NEU-1881). The league page reads the date alone, so a date no team played has empty lists, not a 404: its movers are `player_window_stats`' 10-game windows on the date, a mover that did not move being neither a riser nor a faller; an impact mover's change is his snapshot at the date less that 10 of his team games before, in the first of EPM, DARKO and local BPM with both, named as its `source`; its injury changes compare each team's newest report by the end of the date with its newest by the end of the league's previous game date, so a report filed on a day no one played counts on the next, each read as the team page reads one, a G League line counting as none: a player newly Out or Doubtful, or back from either to Available or off the report (NEU-1882). The game page's timeline is every stint of both teams', garbage time included and marked, a row per player on the floor; its lineups are each team's fives in the game by offensive possessions and follow `?garbage_time=`; its closing lineup is each team's last stint's five when the game was decided by 10 or fewer, null otherwise; an unknown game ID is a 404. `/api/health` and `closinglineup health` apply one rule (`health.py`), a 503 or exit 1 carrying the reason; a database with no final game yet is healthy. The image's `HEALTHCHECK` runs the command, but kumadrome's `app` is checked for readiness alone, since the worker waits on it (NEU-1883).

## 9. Frontend

Routes: `/` (league morning page), `/teams/:tricode`, `/players/:id`, `/games/:gameId`. A date picker in the header sets `?date=` globally. Components: `TierBoard`, `SparklineCell`, `AnnotatedTimeSeries` (multi-series with event markers), `LineupTable`, `AvailabilityBoard`, `StintTimeline` (horizontal gantt per player, both teams), `FlagList`, `ShotProfile`. ECharts is the only charting library; charts fetch nothing themselves, pages do via TanStack Query. Desktop-first; nothing below 1024 px is tested. No auth.

## 10. Configuration (env, read only by `Settings`)

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | — | Postgres DSN |
| `DATA_DIR` | `/data` | raw cache, parquet, pbpstats dir |
| `TZ` | `America/New_York` | all scheduling |
| `NBA_RPS` | `1.0` | NBA.com requests per second |
| `NBA_TIMEOUT_SECONDS` | `30` | |
| `HEALTHCHECKS_URL_NIGHTLY` / `_DAILY` / `_WEEKLY` | unset → no-op | |
| `EPM_ENABLED`, `DARKO_ENABLED`, `HOOPSRUMORS_ENABLED` | `false` until terms are checked | Hoops Rumors replaces `SPOTRAC_ENABLED` (ADR-0004) |
| `GARBAGE_TIME_RULE` | `25:540,20:360,15:180,10:60` | |
| `TIER_STARTER_MIN`, `TIER_CORE_SHARE`, `TIER_FRINGE_PLAYED_RATE` | `4/5`, `0.036`, `0.5` | |
| `LOG_LEVEL` | `INFO` | |
| `SEASON` | derived from today | override for backfill |

## 11. Testing

- Every endpoint has a trimmed recorded fixture under `backend/tests/fixtures/<source>/<endpoint>/`, recorded by `scripts/record_fixtures.py` from kumadrome. Parsers are tested only from fixtures.
- `parse/pbp.py` is tested on full fixture games (one with overtime, one with a known PBP ordering quirk, a 2026-27 feed, and a reconciliation game) against the stint and possession counts pbpstats emits.
- Tier rules, events, flags and garbage time are tested on hand-labelled 2025-26 cases in `tests/derive/cases/*.yml` (player, team, date, expected tier, expected flags, optional expected badges, and a note naming the evidence). At least 30 cases across all five tiers and every flag. `tests/test_derive_cases.py` loads `tests/fixtures/derive/`, five teams' real games through 2026-03-20 with their injury reports and player movement (its README says how it was built), into a database of its own and runs `derive` over it (NEU-1879).
- Reconciliation: 5-man lineups with ≥ 100 possessions match `leaguedashlineups` net rating within 3 possessions' worth (3 × 110 / `POSS`; unfiltered); on-court ratings match `leagueplayerondetails` within ±0.5. Possessions are credited as NBA.com credits them, to every lineup and player on the court during each one, which a stint does not (NEU-1899); at a substitution logged at the instant a possession starts or ends, by the events there rather than the clock, and free-throw points at the foul (NEU-1900, `parse/pbp.py`). The rule matches every five's `POSS` in 24 of 32 games of NBA.com's per-game `leaguedashlineups`, the three fixture games among them, but not all, so lineups keep a tolerance of a few possessions rather than a flat ±0.1.
- API routes have schema tests against a seeded test database. Frontend components have vitest render tests; pages have one smoke test each against mocked API responses.
- CI gate: `uv run ruff check && ruff format --check && pyright && pytest` for `backend/`; `pnpm lint && pnpm tsc -b && pnpm test` for `frontend/`; both run locally before any push.

## 12. Deployment on kumadrome

`deploy/docker-compose.yml`: `postgres` (17, volume on the T7 drive), `app` (FastAPI + static frontend, 127.0.0.1 only), `worker` (same image, `closinglineup worker`), `tailscale` sidecar (userspace, `TS_HOSTNAME=closinglineup`, serves the app on the personal tailnet; never name the container after the service), `watchtower` scoped to closinglineup. `DATA_DIR` and the Postgres volume live under `/mnt/music/closinglineup/`. A systemd timer runs `pg_dump` nightly to B2 like the Navidrome backup. `deploy/README.md` is ported from nba-today and covers the healthchecks.io checks, `.env`, first backfill, and the sudo steps as `ssh -t kumadrome 'sudo …'` commands.

## 13. Milestones

The Linear project (P-NEU-101) is the authoritative list. Stories NEU-1841..1847, one per milestone; work tickets NEU-1848..1893.

| Milestone | Story | Tickets |
|---|---|---|
| M1 Foundation | NEU-1841 | NEU-1848..1852, NEU-1894 (GitHub hardening) |
| M2 Fetcher proves out on kumadrome | NEU-1842 | NEU-1853..1859 |
| M3 Schema and backfill | NEU-1843 | NEU-1860..1865 |
| M4 Overlays | NEU-1844 | NEU-1866..1873 |
| M5 Derived layer | NEU-1845 | NEU-1874..1879 |
| M6 API and UI | NEU-1846 | NEU-1880..1889 |
| M7 Hardening and go-live | NEU-1847 | NEU-1890..1893 |

NEU-1859, NEU-1892 and NEU-1893 need hands-on kumadrome steps and are not `loop-ready`.

## 14. Acceptance criteria for v1

- The nightly job completes unattended on ≥ 95% of nights over a 30-day stretch; misses self-heal on the next run.
- For any team on any 2026-27 date, the team page renders the tier board, closing lineups, top lineups and availability board within 2 s.
- Derived 5-man lineup net ratings (unfiltered) match `leaguedashlineups` within 3 possessions' worth for lineups with ≥ 100 possessions (§11).
- The league page surfaces every rotation event from the prior night with its top three signals.
- Player time series carry injury and transaction annotations with correct dates.
- Zero paid data sources; zero public exposure; no Basketball Reference requests.

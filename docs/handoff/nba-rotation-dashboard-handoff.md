# NBA Rotation & Value Dashboard — Project Handoff

Handoff for Claude Code (Fable). Intended input to the `/projectit` → `/planit` pipeline in `tomboone/claude-skills`. Everything below is scope, constraints, and design intent; break it into a Linear project and issues as the planning skill sees fit.

---

## 1. Purpose

A private, single-user NBA dashboard that answers the questions a casual-but-curious watcher can't get from box scores:

- Whose production is dropping in a way that predicts a loss of minutes or a roster spot?
- Why does a player keep his rotation spot despite weak raw stats?
- Why do certain players fall in and out of the rotation?
- How is a player's value trending — not "who was hot last night," but 5/10/20-game windows against season and prior-season baselines.

Target fidelity: in the ballpark of what a team analyst looks at every morning, using only free public data. It will not rival front-office tooling (no Second Spectrum/Sportradar tracking, no Synergy play types, no medical/load data) and doesn't need to.

**User:** Tom, sole user. No public exposure, no multi-tenancy, no data licensing concerns, no paid data sources.

## 2. Non-goals (v1)

- Fantasy scoring, draft tooling, or anything league-format-specific
- Any predictive/ML model (collect clean data first; models are a later phase)
- Own RAPM/EPM implementation (pull public impact metrics; compute BPM locally as a fallback)
- Play-type classification from play-by-play
- Betting / odds
- Mobile-first UI, auth, or sharing features
- WNBA / G League (keep IDs compatible, but don't build for them)

## 3. Core concept: rotation state, not stats

The dashboard's organizing idea is each player's **rotation state on a given team on a given date** and what drives changes in it. Minutes are the leading indicator; box production lags. Every view should make the minutes trend visible first, then the production/impact trends that explain it.

Rotation tiers (computed, per team, per date, from trailing-window minutes share and starter/closer rates):

| Tier | Definition (initial; tune later) |
|---|---|
| Starter | Started ≥ 4 of last 5 played games |
| Core | ≥ 18% minutes share over last 10 team games, not a starter |
| Fringe | Played in ≥ 50% of last 10 team games, < 18% share |
| Out | DNP-CD or inactive in ≥ 50% of last 10 team games, not injured |
| Injured | Listed Out/Doubtful on current injury report |
| Two-way / G League | Contract type two-way, or currently assigned |

Tier transitions are events. The league-level daily page is primarily a feed of tier transitions and the signals preceding them.

## 4. Data sources

All free. Each has a specific constraint that shapes the architecture.

### 4.1 stats.nba.com (via `nba_api` Python package)

The primary source. Endpoints needed (names as in `nba_api.stats.endpoints`):

- `leaguegamefinder` / `scoreboardv3` — game list per date
- `boxscoretraditionalv3`, `boxscoreadvancedv3`, `boxscorehustlev2`, `boxscorematchupsv3`, `boxscoreusagev3` — per game
- `playbyplayv3` — per game
- `shotchartdetail` — per player-season (heavy; batch weekly, not nightly)
- `commonteamroster`, `commonallplayers` — rosters
- `leaguedashplayerstats`, `leaguedashteamstats`, `leaguedashlineups`, `leagueplayerondetails` — season/window aggregates for cross-checking derived numbers
- `playergamelogs` — backfill convenience

**Hard constraint:** stats.nba.com sits behind Akamai bot protection. It (a) rejects non-browser TLS fingerprints and (b) silently drops connections from datacenter IP ranges (AWS, GCP, Azure, DigitalOcean, Hetzner — effectively all cloud hosts). Requests hang rather than error. Fetching must run from a residential IP. Rate-limit yourself (~1 req/sec with jitter, browser-like headers via `nba_api`'s header support); the package handles headers but not the IP problem.

### 4.2 cdn.nba.com live endpoints (via `nba_api.live`)

Scoreboard, box score, play-by-play per game. No bot protection; work from anywhere. Use as the fallback for PBP/box when stats.nba.com breaks, and as the primary for same-day "is the game final yet" polling.

### 4.3 `pbpstats` Python package (Darryl Blackport)

Parses stats.nba.com / data.nba.com play-by-play into:
- lineup on floor for every event
- possession records (start/end, score margin, how previous possession ended, second-chance, etc.)
- shots/rebounds/assists by zone

This is what makes stints, lineup net ratings, and garbage-time-filtered on/off possible. Use its file-backed data directory so raw responses are cached and hand-fixable (PBP has occasional ordering errors; the package handles common cases).

### 4.4 Basketball Reference (scrape, sparingly)

Use only for things not on stats.nba.com: transactions log, contract/option summaries if Spotrac is painful, historical cross-checks. Hard limit of 20 requests/minute per IP; violators are blocked for up to a day. No API, by policy. Cache aggressively; never put it in the nightly hot path.

### 4.5 Public impact metrics

- **Dunks & Threes EPM** (dunksandthrees.com) — daily-updated; best public impact metric. Scrape the player table daily; store snapshots. Confirm terms of use allow personal scraping before scheduling.
- **DARKO** (Kostya Medvedovsky's Shiny app) — daily-updated projections with CSV export. Verify current availability at build time.
- **BPM** — compute locally from box score + team data (Basketball Reference's published formula) so an impact metric exists even if the above disappear.

### 4.6 Injury report

NBA publishes an official injury report PDF several times daily (URL pattern to confirm at build time). Parse to `player, team, game, status (Out/Doubtful/Questionable/Probable/Available), reason`. This is the single most important "why" overlay — most confusing rotation changes are injuries or returns from injury.

### 4.7 Transactions and contracts

- Transactions: NBA.com transactions page or Basketball Reference transactions log (daily, cached).
- Contracts: Spotrac or RealGM team salary pages (weekly). Need: salary by season, guaranteed amount, option type/date, two-way flag, trade-eligibility date. Approximate is fine.

## 5. Architecture

```
┌──────────────────────── home (residential IP) ────────────────────────┐
│  kumadrome (2011 Mac Mini, Ubuntu, Docker)                             │
│   • fetcher container: nightly cron ~03:30 ET                          │
│       nba_api + pbpstats → raw JSON cache → parquet                    │
│   • injury/transactions/EPM scrapers (daily), contracts (weekly)       │
│   • [option A] Postgres + API + web UI here, reached over Tailscale    │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ rsync / object push (option B only)
┌───────────────────────────────▼────────────────────────────────────────┐
│  [option B] Hetzner / Coolify: Postgres + FastAPI + frontend            │
└────────────────────────────────────────────────────────────────────────┘
```

**Fetch tier must be at home** because of §4.1. kumadrome is the right host (always-on, Docker, residential IP). Do not use the Pi (being retired; crash-prone).

**Serving tier — decision for Tom** (flag as an open question in planning, default to A):
- **A. Everything on kumadrome, access over the personal tailnet.** Zero hosting cost, no public surface, no data shipping, simplest ops. Fits a single-user private tool.
- **B. Fetch at home, serve from Coolify.** Only if Tom wants it reachable without Tailscale or wants it on the neuroticsasquat.ch infra. Note: the Coolify host is designated for neuroticsasquat.ch tools rather than personal apps, which argues for A.

**Stack** (match Tom's defaults):
- Python 3.12+, FastAPI, SQLAlchemy/SQLModel, Alembic
- Postgres (DuckDB acceptable for v1 if it keeps ops lighter; parquet is the interchange format either way)
- Frontend: open — lightweight is preferred. HTMX + a charting lib, or a small React/Vite app. Charts need: multi-series time series with event annotations, sparklines in tables, stint/gantt timeline. Plotly or ECharts both cover this.
- Docker Compose for local and kumadrome; Coolify Compose if option B.
- Observability: ship logs/traces to Tom's existing SigNoz; healthchecks.io ping on nightly job completion.

## 6. Data model

One row per entity-instance; rolling-window stats are **views/materialized views, never stored rows**.

**Reference**
- `team` — nba_team_id, abbr, name, conference, division
- `player` — nba_player_id, name, birthdate, draft year/pick, height, position
- `roster_membership` — player, team, start_date, end_date, contract_type (standard/two-way/10-day/exhibit-10)
- `game` — game_id, date, home/away team, status, final score, is_back_to_back flags per team

**Per game**
- `player_game` — game, player, team, starter flag, minutes (seconds), full traditional + advanced box, plus_minus, dnp_reason (null / CD / injury / inactive / suspension / G League)
- `stint` — game, period, start_sec, end_sec, team, five player IDs (sorted), opponent five, possessions, pts_for, pts_against, is_garbage_time
- `possession` — from pbpstats, as it emits them; keep its schema
- `shot` — game, player, x, y, zone, type, made, assisted_by, shot_clock bucket if available
- `matchup` — game, offensive player, defensive player, partial possessions, points, FGA/FGM (from boxscorematchups)

**Daily / periodic snapshots**
- `injury_status` — report_datetime, game, player, team, status, reason
- `transaction` — date, type (sign/waive/trade/two-way/assign/recall/option), player, from_team, to_team, detail
- `impact_snapshot` — date, player, source (epm/darko/bpm_local), value, o_value, d_value, minutes basis
- `contract` — player, season, salary, guaranteed, option_type, option_date, trade_eligible_date, as_of_date

**Derived (views)**
- `player_window_stats(window ∈ {5,10,20,season,prior_season})` — all metrics in §7 per player-team-date-window
- `rotation_state` — player, team, date, tier, and the inputs behind it
- `rotation_event` — tier transitions with before/after and the top contributing signals

## 7. Metrics (all per window, with delta vs. prior equal window and vs. season)

**Minutes / role (leading indicators)**
- Minutes per game played; minutes share = player minutes ÷ team minutes in games the team played
- Minutes volatility = stdev of minutes over window (single best early-warning signal)
- Starter rate; closer rate = share of games in which player was on floor in final 5 min of a game within 10 pts
- Rotation rank (ordinal by minutes within team)
- DNP-CD count and inactive count

**Efficiency / production**
- TS%, eFG%, usage %, AST%, TOV%, ORB%/DRB%, FTr, 3PAr
- Shot profile: rim / short mid / long mid / 3 frequency and FG% (from `shot`)
- Points, assists, rebounds per 36 and per 100 possessions (de-emphasized in UI)

**On-court impact**
- ORtg / DRtg / NetRtg on court (from stints, garbage-time filtered)
- On/off net rating with possession counts
- Net rating of player's top 3 most-used 2-man combos
- Public impact metric (EPM preferred; DARKO; local BPM) with O/D split

**Defense proxies (where the public tracking subset allows)**
- Opponent FG% when primary defender (matchups)
- Deflections, contested shots, charges (hustle)

**Flags (boolean, derived)**
- `minutes_cliff` — 5-game minutes share < 60% of 20-game share, not injured
- `usage_up_efficiency_down` — usage +3 pts and TS% −4 pts (10g vs. season)
- `bleeding` — on-court net rating ≤ −5 over 10g with ≥ 200 possessions, while team on/off is positive without him
- `earning_trust` — minutes share up 3 consecutive 5-game windows
- `yo_yo` — ≥ 3 tier transitions in 20 games
- `contract_pressure` — non-guaranteed money with a guarantee date within 30 days, or two-way nearing NBA-day limit

## 8. Views

1. **League / morning page**
   - Tier transitions since yesterday, grouped by team
   - Largest 10-game minutes-share movers, up and down (top 15 each)
   - Flags raised today (from §7)
   - Injury status changes, transactions
   - Top/bottom 10 by 10-game impact delta
2. **Team page** (one per team)
   - Roster as tier board with per-player sparklines (minutes share, net on/off, impact)
   - Last 5 games' closing lineups
   - Top 10 lineups by possessions (5-man) and top 10 2-man combos, with net rating and trend
   - Availability board (injury report + G League assignments)
   - Roster count vs. limits; contract notes (non-guaranteed, options, trade eligibility)
3. **Player page**
   - Time series (toggle 5/10/20/season): minutes & share, on/off net, TS% & usage, impact metric — with annotations for injury statuses, transactions, starter/closer changes
   - Shot profile this window vs. season vs. prior season
   - Most-shared-floor teammates and net rating with each
   - Matchup summary (who he guards, who guards him)
   - Flag history
4. **Game page**
   - Rotation chart: horizontal stint timeline per player, both teams
   - Lineup results for the game, closing lineup, garbage-time marker

## 9. Phases / milestones

Suggested breakdown for `/planit`; adjust freely.

**M0 — Fetcher proves out (home only)**
- Docker container on kumadrome with `nba_api` + `pbpstats`; confirm stats.nba.com reachable from that IP
- Fetch one game end-to-end: box (trad/adv/hustle/matchups), PBP, pbpstats possessions + lineups; cache raw JSON
- Nightly cron skeleton with healthchecks.io ping

**M1 — Backfill and schema**
- Postgres schema + migrations for §6
- Backfill 2025–26 full season (baselines) and 2026–27 to date; shot charts weekly batch
- Reference data: teams, players, rosters, roster_membership history for both seasons
- Validation: reconcile derived on-court ratings and on/off against `leaguedashlineups` / `leagueplayerondetails` for a sample of teams

**M2 — Overlays**
- Injury report parser (PDF) + daily snapshot
- Transactions scraper + classifier
- EPM and DARKO daily snapshots; local BPM computation
- Contracts weekly scrape

**M3 — Derived layer**
- Window views, rotation_state, rotation_event, flags
- Unit tests on tier logic and flags against hand-labeled cases from the prior season

**M4 — UI**
- Team page first (highest daily value), then player page, league page, game page
- Chart components: annotated time series, sparkline table, stint timeline

**M5 — Hardening**
- cdn.nba.com fallbacks when stats.nba.com fails; raw-cache reparse command
- Alerting on fetch failures and schema drift (endpoint column changes)
- Serving-tier decision finalized (option A/B); Tailscale access set up

**Later (explicitly out of v1):** predictive minutes model, own RAPM, play-type tagging, playoff handling.

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| stats.nba.com endpoint changes or outage mid-season | Keep raw responses; cdn.nba.com fallback for PBP/box; `nba_api` is actively maintained — pin and upgrade deliberately |
| Home fetch host down (kumadrome) | healthchecks.io alert; fetcher is idempotent and backfills missed dates on next run |
| Injury-report PDF format changes | Parser is a separate module with fixture tests; failure degrades gracefully (stale status, flagged in UI) |
| EPM/DARKO become unavailable or paywalled | Local BPM always computed; `impact_snapshot.source` keeps them interchangeable |
| Basketball Reference block | Never in nightly path; 20 req/min ceiling enforced in client; cache with long TTLs |
| PBP ordering errors corrupt stints | pbpstats fixes common cases; keep its override files in repo; validation step vs. official lineup stats |
| Garbage-time definition skews on/off | Use a conventional rule (e.g., Cleaning the Glass–style margin/time thresholds); make it a config constant |

## 11. Open decisions for Tom

1. Serving tier: option A (kumadrome + Tailscale) vs. B (Coolify). Default A.
2. Postgres vs. DuckDB for v1.
3. Frontend: HTMX vs. React/Vite.
4. Include 2024–25 in backfill for a second baseline season? (Costs fetch time only.)
5. Repo name/location and whether it lives under the neuroticsasquat.ch Linear workspace or a personal one.

## 12. Acceptance criteria for v1

- Nightly job completes unattended ≥ 95% of nights over a 30-day stretch; misses self-heal on the next run
- For any team on any date this season, the team page renders tier board, closing lineups, top lineups, and availability within 2 s
- Derived on-court net ratings match stats.nba.com's `leaguedashlineups` within rounding for 5-man lineups with ≥ 100 possessions
- League page surfaces every tier transition from the prior night with the top 3 contributing signals
- Player page time series carries injury and transaction annotations with correct dates
- Zero paid data sources; zero public exposure

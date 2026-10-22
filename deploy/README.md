# Deploying closinglineup on kumadrome

closinglineup runs on kumadrome as one compose project: `postgres`, `app` (the API and the built UI),
`worker` (the job scheduler, same image) and a Watchtower scoped to this project that pulls new images
from GHCR. kumadrome is the home Mac mini (Ubuntu, Docker with the Compose plugin, residential IP,
which stats.nba.com requires; see ADR-0001). The database and the raw cache live on the USB drive at
`/mnt/music`. The only outside account involved is [healthchecks.io](https://healthchecks.io) (alerting).

Images are published by `.github/workflows/image.yml` on every push to `main`:

| Tag | Use |
|---|---|
| `latest` | what kumadrome runs |
| `sha-<7>` | pinning or rolling back to one commit |

Commands below run on kumadrome (`ssh kumadrome`) unless they start with `ssh`. A step that needs `sudo` is
written as `ssh -t kumadrome 'sudo …'` so it can prompt for the password.

Not here yet: the Tailscale sidecar and the nightly `pg_dump` to B2 (M7). Until the sidecar lands, the UI
is on kumadrome's loopback only; reach it through an SSH tunnel, `ssh -L 8010:127.0.0.1:8010 kumadrome`,
then http://localhost:8010.

## 1. Make sure kumadrome can pull the image

Watchtower pulls anonymously, so the `ghcr.io/tomboone/closinglineup` package must be public: your GitHub
profile → Packages → `closinglineup` → Package settings → Change visibility → Public. Check it from
kumadrome:

```sh
docker pull ghcr.io/tomboone/closinglineup:latest
```

The image is built for `linux/amd64` only, which is what kumadrome runs.

## 2. Fetch the deploy files and make the data directories

Only `deploy/` is needed. A sparse clone fetches that directory (plus the handful of top-level files git
always includes) without the source tree:

```sh
git clone --filter=blob:none --sparse --branch main https://github.com/tomboone/closinglineup.git ~/closinglineup
cd ~/closinglineup
git sparse-checkout set deploy
cd deploy
```

Every command from here on runs in `~/closinglineup/deploy/`. `git pull` picks up later changes to these
files.

The raw cache, parquet and pbpstats files go in `data/`, the Postgres cluster in `pg/`, both on the USB
drive. Every bind mount sets `create_host_path: false`, so if the drive is not mounted the start fails
instead of Docker creating empty directories on the root disk. Both containers run as UID/GID 999 (the
app's user, and the `postgres` image's), so both directories must be owned by it:

```sh
ssh -t kumadrome 'sudo install -d -o 999 -g 999 /mnt/music/closinglineup/data /mnt/music/closinglineup/pg'
```

## 3. Create the healthchecks.io checks

One check per job family. Create all three now. A check that has never been pinged stays **new** and
does not alert, so the `daily` and `weekly` checks wait quietly until their jobs land (M4).

| Check | Pinged by | Period | Grace |
|---|---|---|---|
| `closinglineup-nightly` | `nightly`, 03:30 every day | 1 day | 3 hours |
| `closinglineup-daily` | `injuries` and `daily_overlays`, 09:00–18:30 | 1 day | 6 hours |
| `closinglineup-weekly` | `weekly`, Mondays 04:30 | 7 days | 1 day |

For each one: **Add Check**, set the period and grace, make sure at least one notification channel is
enabled under **Integrations**, and copy its ping URL (`https://hc-ping.com/<uuid>`) for `.env`.

A job pings when it succeeds and pings `/fail`, with the exception summary in the body, when it fails.
`nightly` runs on days without games too. Between seasons it fails, because the CDN still serves the
previous season's schedule, so pause the nightly check over the off-season.

## 4. Configure `.env`

```sh
cp .env.example .env
chmod 600 .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
```

Edit `.env` and paste the three `HEALTHCHECKS_URL_*` values. The data paths already match step 2. Every
variable is described in `.env.example`; the app's full list of settings is in the project spec, §10.

The password is set once, when Postgres creates its cluster in `pg/`. Changing it in `.env` later does not
change the database's password.

## 5. Start it

```sh
docker compose config -q   # validates the compose file and .env; prints nothing on success
docker compose up -d
docker compose ps
```

`app` runs the database migrations (`alembic upgrade head`) before it starts serving, and its healthcheck
requests the API, so it turns healthy only after they have run. `worker` waits for `app` to be healthy, so
it never writes to a schema that is not there yet. Within about a minute
`docker compose ps` shows all four services `healthy`.

## 6. First real fetch

Fetch one 2026-27 final game from the home IP, to prove stats.nba.com answers kumadrome. To list the
regular-season games the schedule shows as final:

```sh
docker compose run --rm -T --no-deps worker python - <<'EOF'
from closinglineup.clock import SystemClock
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import make_nba_session

settings, clock = Settings(), SystemClock()
with make_nba_session(settings, clock) as session:
    cache = RawCache(settings.data_dir, clock)
    schedule = nba_cdn.fetch_schedule(session, cache, "2026-27", force=True)
for day in schedule["leagueSchedule"]["gameDates"]:
    for game in day["games"]:
        if game["gameStatus"] == nba_cdn.GAME_FINAL and game["gameId"].startswith("002"):
            away, home = game["awayTeam"]["teamTricode"], game["homeTeam"]["teamTricode"]
            print(game["gameId"], day["gameDate"][:10], f"{away} @ {home}")
EOF
```

Then fetch every per-game endpoint of one of them:

```sh
docker compose run --rm worker closinglineup fetch --game-id 0022600001
ls /mnt/music/closinglineup/data/raw/nba_stats/*/
```

Seven endpoints report `fetched` and land in `data/raw/nba_stats/<endpoint>/<game_id>.json`.
`playbyplayv2` reports `failed: playbyplayv2 has no data`, and so the command exits 1: stats.nba.com
currently answers `{}` for it on every game, and an empty answer is never cached (see
`nba_stats.fetch_playbyplay_v2`). Anything else that fails, such as a timeout or a connection reset on
every endpoint, means stats.nba.com is not answering kumadrome.

To run the whole nightly job by hand for one date (the schedule refresh, every final game's stats.nba.com
and cdn.nba.com feeds, and a ping to the nightly check):

```sh
docker compose run --rm worker closinglineup nightly --date 2026-10-20
```

## 7. Backfill

The nightly job only fetches the day before, so the cache starts with whatever has been played since the
worker went live. The backfill fills in 2025-26 whole and 2026-27 to date (spec §2): fetch every final
game, fetch the rosters, then parse and load, then build the roster memberships.

**Fetch the games.** `fetch --season` refreshes the season's schedule from stats.nba.com (the CDN serves
only the current season's), then fetches every counted final game not yet cached, oldest first, printing a
line per game:

```sh
tmux new -s backfill
docker compose run --rm worker closinglineup fetch --season 2025-26
docker compose run --rm worker closinglineup fetch --season 2026-27
```

Each game is eight requests (six stats.nba.com endpoints and two cdn.nba.com feeds; `playbyplayv2` is
left out, since it only ever fails) at `NBA_RPS`, so about 8 s a game at the default of 1. 2025-26 has
1,322 counted games, about **3 hours**; 2026-27 a few minutes. Run it in `tmux` (or `screen`) so an SSH
drop does not stop it. The worker's own jobs keep their own rate limit, so while one runs the two together
send about twice `NBA_RPS`: start the 2025-26 fetch after the 03:30 nightly run.

It is safe to stop at any point: Ctrl-C, a dropped connection, a reboot. Run the same command again and it
skips every game already cached, and resumes the game it stopped in at its first endpoint not cached.
`--from 2026-01-01` (and `--to`) restrict it to a range of game dates. A game with an endpoint that failed
is listed with the failure, the run carries on, and the command exits 1 at the end; running it again
retries only what is missing.

**Fetch the rosters** of both seasons (31 requests each). A past season's rosters are as they stood at its
end, so they fill in player details (birthdate, height, position) but add no memberships:

```sh
docker compose run --rm worker closinglineup fetch --rosters --season 2025-26
docker compose run --rm worker closinglineup fetch --rosters --season 2026-27
```

**Parse and load** each season's dates, then build its roster memberships from the games just loaded.
Rosters are snapshots, so the memberships of a past season come from its games: a span per run of games
a player had a box-score line for with one team (`nightly` rebuilds the current season's after each
load). A date with no games parses to empty files, so the ranges can be generous:

```sh
docker compose run --rm worker closinglineup parse --from 2025-10-01 --to 2026-06-30
docker compose run --rm worker closinglineup load --from 2025-10-01 --to 2026-06-30
docker compose run --rm worker closinglineup load --memberships --season 2025-26
docker compose run --rm worker closinglineup parse --from 2026-10-01 --to 2026-10-22
docker compose run --rm worker closinglineup load --from 2026-10-01 --to 2026-10-22
docker compose run --rm worker closinglineup load --memberships --season 2026-27
```

`parse` lists any game it could not read (a play-by-play pbpstats needs an override for, say) and exits 1;
the rest of the date is written. `load --missing` reloads whatever failed to load.

**Check it.** Every final game in the schedule is in `game`:

```sh
docker compose exec postgres psql -U closinglineup -c \
  "SELECT season, count(*) FROM game WHERE status = 'final' GROUP BY season ORDER BY season"
docker compose run --rm -T --no-deps worker python - <<'EOF'
import json
from closinglineup.scheduler import final_games_between
from closinglineup.settings import Settings
from closinglineup.sources import nba_stats
from closinglineup.sources.cache import RawCache

cache = RawCache(Settings().data_dir)
for season in ("2025-26", "2026-27"):
    schedule = json.loads(cache.get(nba_stats.SOURCE, nba_stats.SCHEDULE_LEAGUE_V2, season) or b"{}")
    print(season, len(final_games_between(schedule)))
EOF
```

The two counts match per season. (2026-27's schedule is as of its last `fetch --season`; a nightly run
since then adds games to `game`.)

Every player who played has a line in `playergamelogs`, so the `player_game` rows of players who played
add up to its rows, season type by season type; the DNP rows are on top. Fetch the logs:

```sh
docker compose run --rm -T --no-deps worker python - <<'EOF'
from closinglineup.clock import SystemClock
from closinglineup.settings import Settings
from closinglineup.sources import nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, make_nba_session

settings, clock = Settings(), SystemClock()
cache = RawCache(settings.data_dir, clock)
with make_nba_session(settings, clock) as session:
    for season in ("2025-26", "2026-27"):
        for season_type in nba_stats.SeasonType:
            try:
                logs = nba_stats.fetch_player_game_logs(session, cache, season, season_type, force=True)
            except NbaHttpError as exc:  # a season type not yet played
                print(season, season_type, 0, exc)
                continue
            print(season, season_type, len(logs["resultSets"][0]["rowSet"]))
EOF
docker compose exec postgres psql -U closinglineup -c "
  SELECT g.season, substr(pg.game_id, 3, 1) AS season_type,
         count(*) FILTER (WHERE pg.dnp_reason IS NULL) AS played,
         count(*) FILTER (WHERE pg.dnp_reason IS NOT NULL) AS dnp
  FROM player_game pg JOIN game g USING (game_id)
  GROUP BY 1, 2 ORDER BY 1, 2"
```

`played` for season type `2` matches `Regular Season`, `4` matches `Playoffs` and `5` matches `PlayIn`.
Type `6` is the NBA Cup final, which no `playergamelogs` season type includes. A difference names games to
look at: one `parse` reported as failed, or one whose box score marks a player who did not get on the
floor as played.

Every `player_game` row has a roster membership covering its date; this counts the ones that do not, and
prints 0 for each season:

```sh
docker compose exec postgres psql -U closinglineup -c "
  SELECT g.season, count(*) FILTER (WHERE NOT EXISTS (
    SELECT 1 FROM roster_membership rm
    WHERE rm.player_id = pg.player_id AND rm.team_id = pg.team_id
      AND rm.start_date <= g.game_date AND (rm.end_date IS NULL OR rm.end_date >= g.game_date)
  )) AS uncovered
  FROM player_game pg JOIN game g USING (game_id)
  GROUP BY 1 ORDER BY 1"
```

## Day-to-day

**Logs.** Every container logs structured JSON to stdout:

```sh
docker compose logs -f worker        # jobs
docker compose logs -f app           # migrations and API requests
docker compose logs -f watchtower    # image updates
```

**Health:** `docker compose exec app closinglineup health; echo $?` exits 0 when the database answers.
It is also the image's `HEALTHCHECK`, for both `app` and `worker`.

**Database:** `docker compose exec postgres psql -U closinglineup`. `job_run` holds one row per job run,
with its status and detail.

**Updates** are automatic: Watchtower checks GHCR every 5 minutes and restarts `app` and `worker` on the new
image, removing the old one, and `app` applies any new migration as it starts. Watchtower runs with
`--scope closinglineup`, so it only touches containers labelled with that scope, and leaves `postgres`
alone. To update by hand: `docker compose pull && docker compose up -d`.

Every other Watchtower on the host (nba-today's, mlb-today's) must be scoped too. An unscoped Watchtower
updates every enabled container whatever its scope, and when it starts it stops every other Watchtower on
the host ([Watchtower docs](https://containrrr.dev/watchtower/running-multiple-instances/)).

**Roll back** by pinning a commit: change the `image` tag in `docker-compose.yml` to `sha-<7>` and run
`docker compose up -d`. Watchtower keeps following that tag, which never moves, so updates stop until you
switch back. Migrations do not roll back with the image: a newer schema stays in place. The pin is a local
edit to a tracked file, so drop it with `git restore docker-compose.yml` before the next `git pull`.

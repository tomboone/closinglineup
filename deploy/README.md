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

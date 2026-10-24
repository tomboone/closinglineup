# Deploying closinglineup on kumadrome

closinglineup runs on kumadrome as one compose project: `postgres`, `app` (the API and the built UI),
`worker` (the job scheduler, same image), `tailscale` (the sidecar that serves `app` on the personal
tailnet) and a Watchtower scoped to this project that pulls new images from GHCR. kumadrome is the home
Mac mini (Ubuntu, Docker with the Compose plugin, residential IP, which stats.nba.com requires; see
ADR-0001). The database and the raw cache live on the USB drive at `/mnt/music`, and a systemd timer
on the host dumps the database nightly to B2. The outside accounts involved are
[healthchecks.io](https://healthchecks.io) (the jobs' alerting), the personal tailnet, and the B2 bucket
the Navidrome backup uses; the backup reports to the personal Uptime Kuma on kumadrome.

Images are published by `.github/workflows/image.yml` on every push to `main`:

| Tag | Use |
|---|---|
| `latest` | what kumadrome runs |
| `sha-<7>` | pinning or rolling back to one commit |

Commands below run on kumadrome (`ssh kumadrome`) unless they start with `ssh`. A step that needs `sudo` is
written as `ssh -t kumadrome 'sudo …'` so it can prompt for the password.

The UI is at `https://closinglineup.<tailnet>.ts.net` from any personal-tailnet device. It is also on
kumadrome's loopback, never the LAN; from a device off the tailnet, reach it through an SSH tunnel,
`ssh -L 8010:127.0.0.1:8010 kumadrome`, then http://localhost:8010.

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

The Tailscale sidecar keeps its node key in `tailscale/`, on the drive too, so a recreated container
rejoins as the same node. It runs as root, so the directory stays root's:

```sh
ssh -t kumadrome 'sudo install -d -m 700 /mnt/music/closinglineup/tailscale'
```

## 3. Create the healthchecks.io checks

One check per job family. Create all three now. A check that has never been pinged stays **new** and
does not alert, so the `daily` and `weekly` checks wait quietly until their jobs land (M4).

| Check | Pinged by | Period | Grace |
|---|---|---|---|
| `closinglineup-nightly` | `nightly`, 03:30 every day (and hourly 04:00–12:00 for now, see [Nightly schedule](#nightly-schedule)) | 1 day | 3 hours |
| `closinglineup-daily` | `injuries` and `daily_overlays`, 09:00–18:30 | 1 day | 6 hours |
| `closinglineup-weekly` | `weekly`, Mondays 04:30 | 7 days | 1 day |

For each one: **Add Check**, set the period and grace, make sure at least one notification channel is
enabled under **Integrations**, and copy its ping URL (`https://hc-ping.com/<uuid>`) for `.env`.

A job pings when it succeeds and pings `/fail`, with the exception summary in the body, when it fails.
`nightly` runs on days without games too. Between seasons it fails, because the CDN still serves the
previous season's schedule, so pause the nightly check over the off-season.

## 4. Get a Tailscale auth key

The sidecar is closinglineup's own node on the personal tailnet, `closinglineup`, the same pattern as
Navidrome's. The host itself is on no tailnet, and one `tailscaled` is on one tailnet at a time, so each
stack brings its own sidecar. Never run `tailscale up`, `login` or `switch` on the host for its sake.

1. **Tailnet settings**, already on for Navidrome: in the personal tailnet's admin console, under *DNS*,
   MagicDNS on and *HTTPS Certificates* enabled. Serve needs both for a `*.ts.net` certificate.
2. **An auth key.** *Settings → Keys → Generate auth key*: **not reusable, not ephemeral.** An ephemeral
   node is removed from the tailnet once it goes offline, so after a reboot or a recreate the sidecar
   would come back as a new node, perhaps `closinglineup-1`. Copy it for `.env` (next step). It is used
   once: `TS_AUTH_ONCE` logs in only while the state directory holds no node key.
3. **After the node joins** (step 6), open it in *Machines* and choose *Disable key expiry*. Otherwise it
   drops off the tailnet when the node key expires, after 180 days by default. If you would rather tag the
   node: add `"tag:closinglineup": ["autogroup:admin"]` to the policy's `tagOwners`, generate the key with
   that tag, and set `TS_EXTRA_ARGS=--advertise-tags=tag:closinglineup`. A tagged node's key never expires.

If a node named `closinglineup` is already listed in *Machines*, remove it first, or the new one comes up
as `closinglineup-1`.

## 5. Configure `.env`

```sh
cp .env.example .env
chmod 600 .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
```

Edit `.env`: paste the three `HEALTHCHECKS_URL_*` values and the auth key as `TS_AUTHKEY`. The data and
Tailscale state paths already match step 2. Every variable is described in `.env.example`; the app's full
list of settings is in the project spec, §10.

The password is set once, when Postgres creates its cluster in `pg/`. Changing it in `.env` later does not
change the database's password.

## 6. Start it

```sh
docker compose config -q   # validates the compose file and .env; prints nothing on success
docker compose up -d
docker compose ps
```

`app` runs the database migrations (`alembic upgrade head`) before it starts serving, and its healthcheck
requests the API (`/api/meta`), so it turns healthy only after they have run. `worker` waits for `app` to be healthy, so
it never writes to a schema that is not there yet. Within about a minute
`docker compose ps` shows `postgres`, `app`, `worker` and `watchtower` `healthy`, and `tailscale` up (it
has no healthcheck).

Then bring the tailnet side up:

1. Check the sidecar joined, and that serve points at `app`:

   ```sh
   docker compose exec tailscale tailscale status | head -1
   docker compose exec tailscale tailscale serve status   # https://closinglineup.<tailnet>.ts.net → http://app:8000
   ```

2. Blank `TS_AUTHKEY=` in `.env`. Because of `TS_AUTH_ONCE`, the used key is never needed again.
3. Disable the node's key expiry, or tag it (step 4, item 3).
4. From a personal-tailnet device, open `https://closinglineup.<tailnet>.ts.net`: the league page. The
   first request can take a few seconds while the certificate is issued.

A 502 there shows in `docker compose logs tailscale` as `proxy error: dial tcp <address>:8000`. Just
after `docker compose up` it is `app` still migrating, and clears once `app` is healthy. If it persists
and `<address>` is the sidecar's own (`docker compose exec tailscale hostname -i`), the proxy is dialling
itself: a container's own hostname maps to its own address in `/etc/hosts`, which is why the sidecar's
`hostname` is `closinglineup-tailscale` and must never be `app`.

**Adding the sidecar to a deploy from before it.** The compose file requires `TS_STATE_HOST_DIR`, so
once it is pulled every `docker compose` command fails until `.env` sets it. Make the state directory
(step 2) and get a key (step 4) first, then pull and add the variables in one go, pasting the key where
`<key>` is:

```sh
git pull && cat >>.env <<'EOF'
TS_STATE_HOST_DIR=/mnt/music/closinglineup/tailscale
TS_AUTHKEY=<key>
TS_EXTRA_ARGS=
EOF
docker compose config -q
docker compose up -d
```

and carry on from item 1 above. `up -d` only creates `tailscale`; the other services are unchanged.

## 7. First real fetch

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

## 8. Backfill

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
send about twice `NBA_RPS`: start the 2025-26 fetch after the morning's nightly runs (see
[Nightly schedule](#nightly-schedule)).

It is safe to stop at any point: Ctrl-C, a dropped connection, a reboot. Run the same command again and it
skips every game already cached, and resumes the game it stopped in at its first endpoint not cached.
A few hundred games into a run, stats.nba.com tends to stop answering: it holds requests open with nothing
sent until they time out. A connection that has started doing that never recovers, while a new one is
answered at once, so each timed-out request is tried again on a fresh connection (with fresh cookies),
four tries in all. If none is answered, the run waits it out, printing
`… no answer from NBA.com, trying again in 5 min` and trying the game again after 5, 15 and 30 minutes;
a game that gets through starts the next wait back at 5. Only if NBA.com is still silent after the
30-minute wait does the run stop, with `stopped: game … no answer from …`, and exit 1; the same command
resumes it. If it stops like that more than once, wait a few hours, or lower `NBA_RPS` in `.env` (0.5
doubles the run time).
`--from 2026-01-01` (and `--to`) restrict it to a range of game dates. A game with an endpoint that failed
is listed with the failure, the run carries on, and the command exits 1 at the end; running it again
retries only what is missing.

**Fetch the rosters** of both seasons (31 requests each). A past season's rosters are as they stood at its
end, so they fill in player details (birthdate, height, position) but add no memberships:

```sh
docker compose run --rm worker closinglineup fetch --rosters --season 2025-26
docker compose run --rm worker closinglineup fetch --rosters --season 2026-27
```

**Parse and load** each season's dates, store the transactions, then build the roster memberships.
Rosters are snapshots, so `load --memberships` builds every season's memberships from the rosters, the
transactions (NBA.com's player movement, every move since 2015) and the games just loaded together, and
replaces the whole table (`nightly` rebuilds it after each load). A date with no games parses to empty
files, so the ranges can be generous:

```sh
docker compose run --rm worker closinglineup parse --from 2025-10-01 --to 2026-06-30
docker compose run --rm worker closinglineup load --from 2025-10-01 --to 2026-06-30
docker compose run --rm worker closinglineup parse --from 2026-10-01 --to 2026-10-22
docker compose run --rm worker closinglineup load --from 2026-10-01 --to 2026-10-22
docker compose run --rm worker closinglineup overlays transactions
docker compose run --rm worker closinglineup load --memberships
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

## 9. Nightly backup to B2

`backup/closinglineup-backup.sh` runs nightly at 05:30 Eastern (+ up to 15 min) from a systemd timer on
the host, as `tom`, after the 03:30 nightly job and Monday's 04:30 weekly. It:

- refuses to run unless `/mnt/music` is mounted;
- runs `pg_dump --format=custom` in the `postgres` container (`docker compose exec -T`), into a dated
  `closinglineup-<YYYYmmdd-HHMMSS>.dump` in `/mnt/music/closinglineup/backups`, checks it with
  `pg_restore --list`, and deletes dumps older than `KEEP_DAYS` (14);
- `rclone sync`s that directory to `closinglineup-backups/` in the B2 bucket the Navidrome backup uses.
  The `b2` remote has `hard_delete = false`, so a dump aged out stays recoverable per the bucket's
  lifecycle;
- pings a push monitor on the personal Kuma: `up` after a good run, `down` with the reason on failure.

| File | Purpose |
|---|---|
| `backup/closinglineup-backup.sh` | The job. POSIX `sh`. Reads `~/.config/closinglineup-backup.env`. |
| `backup/closinglineup-backup.env.example` | Its settings: the paths, `KEEP_DAYS`, the B2 destination, and `PING_URL`. |
| `backup/closinglineup-backup.service`, `.timer` | Oneshot service as `tom`, with `RequiresMountsFor=/mnt/music`. `Persistent` catches up after downtime. |

### Install

rclone and the `b2` remote are already on kumadrome, from the Navidrome backup (`rclone listremotes`
lists `b2:`).

1. **The dump directory**, on the drive, owned by `tom`:

   ```sh
   ssh -t kumadrome 'sudo install -d -o tom -g tom /mnt/music/closinglineup/backups'
   ```

2. **A push monitor** on the personal Kuma (`http://127.0.0.1:3002` on kumadrome): *Add New Monitor*,
   type *Push*, name `kumadrome/closinglineup-backup`, heartbeat interval 25 h (90000 s), retries 0. Copy
   its push URL.
3. **Settings.** Fill in `<bucket>` (the Navidrome backup's, which `grep REMOTE
   ~/.config/navidrome-backup.env` shows) and the push URL's `<token>`. Keep `PING_URL` in double
   quotes: the file is sourced, and an unquoted `&` from Kuma's default `?status=up&msg=OK&ping=` query
   would leave it unset and skip the ping silently.

   ```sh
   cp backup/closinglineup-backup.env.example ~/.config/closinglineup-backup.env
   chmod 600 ~/.config/closinglineup-backup.env
   $EDITOR ~/.config/closinglineup-backup.env
   ```

4. **Timer**, and a first run in the foreground:

   ```sh
   ssh -t kumadrome 'sudo cp ~tom/closinglineup/deploy/backup/closinglineup-backup.service ~tom/closinglineup/deploy/backup/closinglineup-backup.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now closinglineup-backup.timer'
   ssh -t kumadrome 'sudo systemctl start closinglineup-backup.service'
   systemctl status closinglineup-backup.service --no-pager; journalctl -u closinglineup-backup --since -1h
   systemctl list-timers closinglineup-backup.timer
   ls -l /mnt/music/closinglineup/backups
   . ~/.config/closinglineup-backup.env && rclone ls "$REMOTE"
   ```

   The dump is in both listings, and `kumadrome/closinglineup-backup` shows an `up` heartbeat.

The units run the script from this clone, so a `git pull` updates the job. After a change to a unit file,
copy it again and `daemon-reload`.

### Restore

A dump from B2 first, if the drive's copy is gone:
`rclone copy "$REMOTE/closinglineup-<ts>.dump" /mnt/music/closinglineup/backups/`.

**Drill** into a scratch database, which leaves the live one alone. The counts match the live
database's as of the dump:

```sh
dump=$(ls -t /mnt/music/closinglineup/backups/closinglineup-*.dump | head -1)
docker compose exec -T postgres createdb -U closinglineup restore_drill
docker compose exec -T postgres pg_restore -U closinglineup -d restore_drill --no-owner --exit-on-error <"$dump"
docker compose exec -T postgres psql -U closinglineup -d restore_drill -c \
  "SELECT (SELECT count(*) FROM game) AS games, (SELECT count(*) FROM player_game) AS player_games,
          (SELECT version_num FROM alembic_version) AS revision"
docker compose exec -T postgres dropdb -U closinglineup restore_drill
```

**For real**, stop the writers, restore over the live database in one transaction, and start them again.
`app` migrates on start, so a dump from an older revision is brought up to head:

```sh
docker compose stop worker app
docker compose exec -T postgres pg_restore -U closinglineup -d closinglineup --clean --if-exists \
  --no-owner --single-transaction --exit-on-error <"$dump"
docker compose start app worker
```

## Nightly schedule

`nightly` runs for the day before at 03:30 and, for now, again every hour from 04:00 through 12:00
Eastern (`scheduler.nightly_trigger`). The hourly runs are a measurement, not the schedule: NEU-1918
replaces them with the final one.

**Why.** At 03:30, stats.nba.com has not yet published the hustle box (`boxscorehustlev2`) or the
matchups (`boxscorematchupsv3`) of any game: the first three 03:30 runs (`job_run` 10, 28 and 53, for
2026-10-21 to 10-23) list both as failed for every game. They had landed by 20:29 Eastern on 10-22, when a
manual `fetch --season` got them; when in between, nothing recorded.

**What each run does.** Every run catches up. A game of its date, or of the `NIGHTLY_LOOKBACK_DAYS`
(default 3) before it, that an earlier run left with endpoints missing has just those requested again
(`playbyplayv2` aside), and its date is parsed and loaded again. The run's `detail -> 'caught_up'` lists
each game that got one, with its endpoints. A run with nothing left to fetch requests the schedule alone:
a date already reconciled is not reconciled again, though one whose reconciliation was skipped (NBA.com's
lineups failed to refresh, say) is, on the next run. So a re-run costs a schedule refresh, a request per
endpoint still missing, and a derive of the date. A game whose late
endpoints have not come by the end of the lookback is left `stats_failed` for `fetch --missing`.

**Reading the measurement.** The first run that lists an endpoint under `caught_up` is the hour it landed:

```sql
select (started_at at time zone 'America/New_York')::timestamp(0) as ran,
       c->>'date' as game_date, c->'endpoints' as endpoints, count(*) as games
from job_run, jsonb_array_elements(detail -> 'caught_up') as c
where job = 'nightly'
group by 1, 2, 3
order by 1;
```

After at least seven nights, NEU-1918 picks (a) 03:30 and one later run, (b) a single later run, or
(c) 03:30 alone, and replaces this section with the choice and these numbers.

**While it lasts.** Every run pings the nightly check, so a failed run alerts and the next good one
clears it. To count `nightly` successes (the NEU-1893 soak), count the 03:30 runs alone:
`… and extract(hour from started_at at time zone 'America/New_York') = 3`. The 05:30 backup can overlap a
re-run, which is harmless: `pg_dump` reads a consistent snapshot.

## Day-to-day

**Logs.** Every container logs structured JSON to stdout:

```sh
docker compose logs -f worker        # jobs
docker compose logs -f app           # migrations and API requests
docker compose logs -f watchtower    # image updates
docker compose logs -f tailscale     # the tailnet node and serve
journalctl -u closinglineup-backup   # the nightly backup
```

**Health:** `docker compose exec app closinglineup health; echo $?` exits 0 when the database answers
and the latest derived date is within 36 h of the latest final game, and prints the reason otherwise;
`/api/health` applies the same rule, answering 503 with the reason. It is the image's `HEALTHCHECK`,
so `worker` turns unhealthy when derive falls behind. `app` overrides it with the readiness check
above, since `worker` waits on `app` and would otherwise never start to catch up a stale database.

**Database:** `docker compose exec postgres psql -U closinglineup`. `job_run` holds one row per job run,
with its status and detail.

**Updates** are automatic: Watchtower checks GHCR every 5 minutes and restarts `app` and `worker` on the new
image, removing the old one, and `app` applies any new migration as it starts. Watchtower runs with
`--scope closinglineup`, so it only touches containers labelled with that scope, and leaves `postgres`
and `tailscale` alone. To update by hand, the sidecar included: `docker compose pull && docker compose up -d`.
A sidecar restart interrupts tailnet access for a few seconds; the loopback port is not affected.

Every other Watchtower on the host (nba-today's, mlb-today's) must be scoped too. An unscoped Watchtower
updates every enabled container whatever its scope, and when it starts it stops every other Watchtower on
the host ([Watchtower docs](https://containrrr.dev/watchtower/running-multiple-instances/)).

**Roll back** by pinning a commit: change the `image` tag in `docker-compose.yml` to `sha-<7>` and run
`docker compose up -d`. Watchtower keeps following that tag, which never moves, so updates stop until you
switch back. Migrations do not roll back with the image: a newer schema stays in place. The pin is a local
edit to a tracked file, so drop it with `git restore docker-compose.yml` before the next `git pull`.

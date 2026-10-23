"""The worker's jobs, on APScheduler: each runs the functions the CLI does and records a `job_run`
row as it starts and finishes (spec §3.3, §6.4).

So far `nightly` refreshes the schedule, fetches, parses and loads, rebuilds the season's roster
memberships from its games, then reconciles a sample of two teams' stints with NBA.com's; the
rest of derive joins it when it lands.
`weekly` re-fetches, parses and loads the season's shot charts.
"""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from apscheduler.executors.pool import (  # pyright: ignore[reportMissingTypeStubs]
    ThreadPoolExecutor,
)
from apscheduler.schedulers.background import (  # pyright: ignore[reportMissingTypeStubs]
    BackgroundScheduler,
)
from apscheduler.triggers.combining import OrTrigger  # pyright: ignore[reportMissingTypeStubs]
from apscheduler.triggers.cron import CronTrigger  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import Clock
from closinglineup.db.models import Game, JobRun, PlayerGame, RunStatus
from closinglineup.derive.reconcile import (
    ReconcileError,
    describe,
    reconcile,
    sample_teams,
    season_type_of,
)
from closinglineup.heartbeat import Heartbeat, JobFamily
from closinglineup.load.loader import load_dates, load_keyed, raise_for_failures
from closinglineup.load.memberships import load_participation_spans
from closinglineup.load.parquet import ParquetStore
from closinglineup.pipeline import SHOT_TABLE, parse_date, parse_shot_charts
from closinglineup.season import Phase, is_counted, season_label
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, NbaUnreachableError
from closinglineup.sources.nba_stats import (
    EndpointResult,
    FetchStatus,
    GameFetch,
    SeasonType,
    fetch_game,
    fetch_player_shots,
    fetch_reconciliation,
)
from closinglineup.teams import by_nba_team_id

NIGHTLY = "nightly"
POLL_FINALS = "poll_finals"
WEEKLY = "weekly"

# The season type each game phase's shots are charted under. The Cup quarterfinals and semifinals
# are regular-season games; the final (season type 6) is in no season type `shotchartdetail` is
# fetched for, so its shots are not fetched.
SHOT_SEASON_TYPES: dict[str, SeasonType] = {
    Phase.REGULAR: SeasonType.REGULAR_SEASON,
    Phase.CUP_KNOCKOUT: SeasonType.REGULAR_SEASON,
    Phase.PLAY_IN: SeasonType.PLAY_IN,
    Phase.PLAYOFF: SeasonType.PLAYOFFS,
}

# The schedule's `gameDates[].gameDate`, an Eastern date, e.g. `10/20/2026 00:00:00`.
SCHEDULE_DATE_FORMAT = "%m/%d/%Y %H:%M:%S"

# The cdn.nba.com per-game feeds, fetched beside `fetch_game`'s stats.nba.com ones; `parse/pbp.py`
# reads the play-by-play.
CDN_GAME_ENDPOINTS: dict[str, GameFetch] = {
    nba_cdn.BOXSCORE: nba_cdn.fetch_live_boxscore,
    nba_cdn.PLAYBYPLAY: nba_cdn.fetch_live_pbp,
}

type Detail = dict[str, Any]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Game:
    game_id: str
    game_subtype: str


def final_games_between(
    schedule: dict[str, Any], start: date | None = None, end: date | None = None
) -> list[tuple[date, str]]:
    """The counted games the schedule lists as final from `start` through `end`, either of them
    open when None, as (game date, game ID), sorted."""
    found: list[tuple[date, str]] = []
    for game_date in schedule["leagueSchedule"]["gameDates"]:
        day = datetime.strptime(game_date["gameDate"], SCHEDULE_DATE_FORMAT).date()
        if (start is not None and day < start) or (end is not None and day > end):
            continue
        found.extend(
            (day, game["gameId"])
            for game in game_date["games"]
            if game["gameStatus"] == nba_cdn.GAME_FINAL
            and is_counted(_Game(game["gameId"], game["gameSubtype"]))
        )
    return sorted(found)


def final_games(schedule: dict[str, Any], day: date) -> set[str]:
    """The counted games the schedule lists as final on `day`."""
    return {game_id for _, game_id in final_games_between(schedule, day, day)}


# Left out of "fully cached": stats.nba.com answers `{}` for every game, so it is never cached
# (see `nba_stats.fetch_playbyplay_v2`), and counting it would re-fetch every game on every run.
NEVER_CACHED = frozenset({nba_stats.PLAYBYPLAY_V2})


def fully_cached(cache: RawCache, game_id: str) -> bool:
    """Whether every per-game endpoint of `game_id` but `NEVER_CACHED` is in the cache."""
    return all(
        cache.exists(nba_stats.SOURCE, endpoint, game_id)
        for endpoint in nba_stats.GAME_ENDPOINTS
        if endpoint not in NEVER_CACHED
    ) and all(cache.exists(nba_cdn.SOURCE, endpoint, game_id) for endpoint in CDN_GAME_ENDPOINTS)


def shot_chart_players(db: sessionmaker[Session], season: str) -> list[tuple[int, SeasonType]]:
    """Every player who has played in `season`, with each season type he has played in, sorted:
    the shot charts `weekly` fetches. A player who has not played has no shots, and stats.nba.com
    answers his shot chart with an empty shell, which fails the fetch."""
    with db() as session:
        played = session.execute(
            select(PlayerGame.player_id, Game.phase)
            .join(Game, Game.game_id == PlayerGame.game_id)
            .where(Game.season == season, PlayerGame.seconds > 0)
            .distinct()
        )
        return sorted({(player_id, SHOT_SEASON_TYPES[phase]) for player_id, phase in played})


def _fetch_cdn_game(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> list[EndpointResult]:
    """`fetch_game` for the cdn.nba.com feeds: a feed that answers badly is FAILED, not raised."""
    results: list[EndpointResult] = []
    for endpoint, fetch in CDN_GAME_ENDPOINTS.items():
        cached = not force and cache.exists(nba_cdn.SOURCE, endpoint, game_id)
        try:
            fetch(session, cache, game_id, force=force)
        except NbaHttpError as exc:
            results.append(EndpointResult(endpoint, game_id, FetchStatus.FAILED, str(exc)))
            continue
        status = FetchStatus.CACHED if cached else FetchStatus.FETCHED
        results.append(EndpointResult(endpoint, game_id, status))
    return results


def fetch_final_game(
    session: NbaSession,
    cache: RawCache,
    game_id: str,
    *,
    force: bool = False,
    skip: Collection[str] = (),
) -> list[EndpointResult]:
    """Every stats.nba.com per-game endpoint of a final game but `skip`, then its cdn.nba.com
    feeds, each reported as `fetch_game` reports it."""
    return [
        *fetch_game(session, cache, game_id, force=force, skip=skip),
        *_fetch_cdn_game(session, cache, game_id, force=force),
    ]


@dataclass
class Jobs:
    """Every job, with what they share; the worker schedules them and the CLI runs them once.

    `nba` opens a fresh `NbaSession` for each run, which the run closes.
    """

    settings: Settings
    clock: Clock
    cache: RawCache
    store: ParquetStore
    nba: Callable[[], NbaSession]
    db: sessionmaker[Session]
    heartbeat: Heartbeat
    # Games `poll_finals` has seen go final, by game date, until `nightly` has run for the date.
    finals: dict[date, set[str]] = field(default_factory=dict[date, set[str]])

    def nightly(self, day: date) -> Detail:
        """Refresh the schedule, fetch every per-game endpoint of each counted game final on `day`
        that is not yet fully cached, then parse and load the date.

        A game is final if the schedule says so or `poll_finals` saw it go final. An endpoint that
        answers badly is listed in the run's detail and does not fail the job, since
        `playbyplayv2` currently always does (see `nba_stats.fetch_playbyplay_v2`); nor does a
        game a parser cannot read, which `parse_date` leaves out. Each earlier parsed date whose
        last load of a table failed or never ran is loaded again, and the season's roster
        memberships are rebuilt from its loaded games. A table that fails to load fails the job,
        and its failed `load_log` row is there for the next run.

        Last, two teams' stints, a different two each day (`reconcile.sample_teams`), are
        reconciled with NBA.com's lineups and on/off, re-fetched for the purpose. A row outside
        tolerance is logged as a warning and listed in the detail; it does not fail the job, nor
        does a response that failed to fetch and is not cached.
        """
        return self._run(NIGHTLY, lambda: self._nightly(day), family="nightly")

    def nightly_yesterday(self) -> Detail:
        """`nightly` for yesterday in `TZ`, as the worker runs it at 03:30."""
        today = self.clock.now().astimezone(self.settings.tz).date()
        return self.nightly(today - timedelta(days=1))

    def weekly(self) -> Detail:
        """Re-fetch the shot chart of every player who has played this season, in each season
        type he has played in, then parse and load them (spec §3.3).

        A shot chart that fails to fetch or parse is listed in the run's detail and does not fail
        the job; whatever was loaded from it before stays. Every shot chart failing to fetch fails
        the job, as does a shot file that fails to load: one with a shot in a game that has no
        `game` row yet, for instance.
        """
        return self._run(WEEKLY, self._weekly, family="weekly")

    def poll_finals(self) -> Detail:
        """Note the games today's scoreboard shows final, for `nightly` to fetch."""
        return self._run(POLL_FINALS, self._poll_finals)

    def _nightly(self, day: date) -> Detail:
        with self.nba() as session:
            schedule = nba_cdn.fetch_schedule(session, self.cache, season_label(day), force=True)
            noted = {g for g in self.finals.get(day, set()) if is_counted(_Game(g, ""))}
            games = sorted(final_games(schedule, day) | noted)
            pending = [game_id for game_id in games if not fully_cached(self.cache, game_id)]
            results = [r for g in pending for r in fetch_final_game(session, self.cache, g)]
            # The season type of the date's games; None for a date with none counted in one.
            season_type = season_type_of(games[-1]) if games else None
            teams = sample_teams(day)
            refresh: list[EndpointResult] = []
            if season_type is not None:
                try:
                    refresh = fetch_reconciliation(
                        session, self.cache, season_label(day), season_type, teams, force=True
                    )
                except NbaUnreachableError as exc:
                    # The date's games are fetched; reconciliation must not hold back their load.
                    refresh = [
                        EndpointResult("reconcile", day.isoformat(), FetchStatus.FAILED, str(exc))
                    ]
            results += refresh
        failed: list[Detail] = [
            {"endpoint": r.endpoint, "key": r.key, "error": r.error}
            for r in results
            if r.status is FetchStatus.FAILED
        ]
        self.finals.pop(day, None)
        parsed = parse_date(self.cache, self.store, day, self.settings.tz)
        loaded = load_dates(self.db, self.store, self.clock, [day])
        # Self-heals any earlier date a past run left without a successful load (spec §3.2).
        earlier = [d for d in self.store.dates() if d != day]
        healed = load_dates(self.db, self.store, self.clock, earlier, missing=True)
        spans = load_participation_spans(self.db, season_label(day))
        raise_for_failures([*loaded, *healed, spans])
        reconciled = (
            self._reconcile(season_label(day), season_type, teams, refresh)
            if season_type is not None
            else None
        )
        return {
            "date": day.isoformat(),
            "games": games,
            "fetched": pending,
            "failed": failed,
            "parsed": parsed.parsed,
            "parse_failed": parsed.failed,
            "parse_missing": parsed.missing,
            "loaded": {result.table: result.rows for result in loaded},
            "healed": [f"{result.table} {result.key}" for result in healed],
            "memberships": spans.rows,
            "reconcile": reconciled,
        }

    def _reconcile(
        self,
        season: str,
        season_type: SeasonType,
        teams: tuple[int, ...],
        refresh: list[EndpointResult],
    ) -> Detail:
        """Reconcile `teams`, or skip it, with a warning, if `refresh` failed: a copy cached
        earlier covers fewer games than the stints do, so every row would read as outside."""
        detail: Detail = {
            "season_type": season_type.slug,
            "teams": [by_nba_team_id(team_id).tricode for team_id in teams],
        }
        try:
            if failed := next((r for r in refresh if r.status is FetchStatus.FAILED), None):
                raise ReconcileError(f"{failed.endpoint} {failed.key} failed to refresh")
            report = reconcile(self.db, self.cache, season, season_type, teams)
        except ReconcileError as exc:
            log.warning("reconciliation skipped", extra={"reason": str(exc)})
            return {**detail, "skipped": str(exc)}
        outside = [describe(check) for check in report.outside]
        for line in outside:
            log.warning("reconciliation outside tolerance", extra={"check": line})
        return {
            **detail,
            "lineups": len(report.lineups),
            "on_court": len(report.on_court),
            "outside": outside,
        }

    def _weekly(self) -> Detail:
        season = season_label(self.clock.now().astimezone(self.settings.tz).date())
        players = shot_chart_players(self.db, season)
        with self.nba() as session:
            results = [
                result
                for player_id, season_type in players
                for result in fetch_player_shots(
                    session, self.cache, player_id, season, season_type, force=True
                )
            ]
        fetched = [r.key for r in results if r.status is not FetchStatus.FAILED]
        if results and not fetched:
            raise NbaHttpError(f"every one of {len(results)} shot charts failed to fetch")
        parsed = parse_shot_charts(self.cache, self.store, fetched)
        loaded = load_keyed(self.db, self.store, self.clock, SHOT_TABLE, parsed.rows)
        raise_for_failures(loaded)
        return {
            "season": season,
            "shot_charts": len(players),
            "fetched": len(fetched),
            "failed": [
                {"key": r.key, "error": r.error} for r in results if r.status is FetchStatus.FAILED
            ],
            "parse_failed": parsed.failed,
            "rows": sum(r.rows for r in loaded),
            "changed": sum(r.changed for r in loaded),
            "removed": sum(r.removed for r in loaded),
        }

    def _poll_finals(self) -> Detail:
        with self.nba() as session:
            scoreboard = nba_cdn.fetch_todays_scoreboard(session, self.cache)["scoreboard"]
        day = date.fromisoformat(scoreboard["gameDate"])
        finals = {
            game["gameId"]
            for game in scoreboard["games"]
            if game["gameStatus"] == nba_cdn.GAME_FINAL
        }
        self.finals.setdefault(day, set()).update(finals)
        return {"date": day.isoformat(), "finals": sorted(finals)}

    def _run(self, job: str, body: Callable[[], Detail], family: JobFamily | None = None) -> Detail:
        """Run `body` between its `job_run` row's start and finish, and ping `family`'s heartbeat.

        Anything `body` raises marks the run failed, pings `/fail` with the exception summary, and
        is re-raised, so APScheduler logs it and the CLI exits 1.
        """
        run_id: int | None = None
        try:
            run_id = self._start(job)
            detail = body()
            self._finish(run_id, RunStatus.SUCCEEDED, detail)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if family is not None:
                self.heartbeat.ping(family, fail=True, reason=reason)
            if run_id is not None:
                self._record_failure(job, run_id, reason)
            raise
        if family is not None:
            self.heartbeat.ping(family)
        log.info("job succeeded", extra={"job": job})
        return detail

    def _start(self, job: str) -> int:
        run = JobRun(job=job, started_at=self.clock.now(), status=RunStatus.RUNNING)
        with self.db.begin() as session:
            session.add(run)
        return run.id

    def _finish(self, run_id: int, status: RunStatus, detail: Detail) -> None:
        with self.db.begin() as session:
            run = session.get_one(JobRun, run_id)
            run.status = status
            run.finished_at = self.clock.now()
            run.detail = detail

    def _record_failure(self, job: str, run_id: int, reason: str) -> None:
        """Mark the run failed; a database that is down too must not hide the job's own error."""
        try:
            self._finish(run_id, RunStatus.FAILED, {"error": reason})
        except SQLAlchemyError:
            log.exception("could not record failed job run", extra={"job": job, "run_id": run_id})


def nightly_trigger(tz: tzinfo) -> CronTrigger:
    return CronTrigger(hour=3, minute=30, timezone=tz)


def weekly_trigger(tz: tzinfo) -> CronTrigger:
    return CronTrigger(day_of_week="mon", hour=4, minute=30, timezone=tz)


def poll_finals_trigger(tz: tzinfo) -> OrTrigger:
    """Every 30 minutes from 18:00 through 02:00."""
    return OrTrigger(
        [
            CronTrigger(hour="18-23,0-1", minute="0,30", timezone=tz),
            CronTrigger(hour=2, minute=0, timezone=tz),
        ]
    )


def build_scheduler(jobs: Jobs) -> BackgroundScheduler:
    """The worker's scheduler, in `TZ`, with every job registered; `start()` runs it in a thread.

    Not a `BlockingScheduler`: its main loop sits in `Event.wait()`, so a SIGTERM handler that
    stops it would run while the main thread holds the lock that stopping needs.
    """
    tz = jobs.settings.tz
    scheduler = BackgroundScheduler(
        timezone=tz,
        # One thread, so no two jobs ever run at once and the NBA.com rate limit holds across them.
        executors={"default": ThreadPoolExecutor(1)},
    )
    # APScheduler 3 is unannotated, so pyright cannot see `add_job`'s parameter types.
    for job, func, trigger in [
        (NIGHTLY, jobs.nightly_yesterday, nightly_trigger(tz)),
        (POLL_FINALS, jobs.poll_finals, poll_finals_trigger(tz)),
        (WEEKLY, jobs.weekly, weekly_trigger(tz)),
    ]:
        # No misfire grace limit: APScheduler drops a run that starts more than its grace (1 s by
        # default) late, and on one thread a job due while another runs always starts late.
        scheduler.add_job(  # pyright: ignore[reportUnknownMemberType]
            func,
            trigger,
            id=job,
            name=job,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )
    return scheduler

"""The worker's jobs, on APScheduler: each runs the functions the CLI does and records a `job_run`
row as it starts and finishes (spec §3.3, §6.4).

So far `nightly` only refreshes the schedule and fetches; parse, load and derive join it as they
land.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any

from apscheduler.executors.pool import (  # pyright: ignore[reportMissingTypeStubs]
    ThreadPoolExecutor,
)
from apscheduler.schedulers.background import (  # pyright: ignore[reportMissingTypeStubs]
    BackgroundScheduler,
)
from apscheduler.triggers.combining import OrTrigger  # pyright: ignore[reportMissingTypeStubs]
from apscheduler.triggers.cron import CronTrigger  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import Clock
from closinglineup.db.models import JobRun
from closinglineup.heartbeat import Heartbeat, JobFamily
from closinglineup.season import is_counted, season_label
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession
from closinglineup.sources.nba_stats import EndpointResult, FetchStatus, GameFetch, fetch_game

NIGHTLY = "nightly"
POLL_FINALS = "poll_finals"

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


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _Game:
    game_id: str
    game_subtype: str


def final_games(schedule: dict[str, Any], day: date) -> set[str]:
    """The counted games the schedule lists as final on `day`."""
    found: set[str] = set()
    for game_date in schedule["leagueSchedule"]["gameDates"]:
        if datetime.strptime(game_date["gameDate"], SCHEDULE_DATE_FORMAT).date() != day:
            continue
        for game in game_date["games"]:
            if game["gameStatus"] == nba_cdn.GAME_FINAL and is_counted(
                _Game(game["gameId"], game["gameSubtype"])
            ):
                found.add(game["gameId"])
    return found


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


def _fetch_cdn_game(session: NbaSession, cache: RawCache, game_id: str) -> list[EndpointResult]:
    """`fetch_game` for the cdn.nba.com feeds: a feed that answers badly is FAILED, not raised."""
    results: list[EndpointResult] = []
    for endpoint, fetch in CDN_GAME_ENDPOINTS.items():
        cached = cache.exists(nba_cdn.SOURCE, endpoint, game_id)
        try:
            fetch(session, cache, game_id)
        except NbaHttpError as exc:
            results.append(EndpointResult(endpoint, game_id, FetchStatus.FAILED, str(exc)))
            continue
        status = FetchStatus.CACHED if cached else FetchStatus.FETCHED
        results.append(EndpointResult(endpoint, game_id, status))
    return results


@dataclass
class Jobs:
    """Every job, with what they share; the worker schedules them and the CLI runs them once.

    `nba` opens a fresh `NbaSession` for each run, which the run closes.
    """

    settings: Settings
    clock: Clock
    cache: RawCache
    nba: Callable[[], NbaSession]
    db: sessionmaker[Session]
    heartbeat: Heartbeat
    # Games `poll_finals` has seen go final, by game date, until `nightly` has run for the date.
    finals: dict[date, set[str]] = field(default_factory=dict[date, set[str]])

    def nightly(self, day: date) -> Detail:
        """Refresh the schedule, then fetch every per-game endpoint of each counted game final on
        `day` that is not yet fully cached.

        A game is final if the schedule says so or `poll_finals` saw it go final. An endpoint that
        answers badly is listed in the run's detail and does not fail the job, since
        `playbyplayv2` currently always does (see `nba_stats.fetch_playbyplay_v2`).
        """
        return self._run(NIGHTLY, lambda: self._nightly(day), family="nightly")

    def nightly_yesterday(self) -> Detail:
        """`nightly` for yesterday in `TZ`, as the worker runs it at 03:30."""
        today = self.clock.now().astimezone(self.settings.tz).date()
        return self.nightly(today - timedelta(days=1))

    def poll_finals(self) -> Detail:
        """Note the games today's scoreboard shows final, for `nightly` to fetch."""
        return self._run(POLL_FINALS, self._poll_finals)

    def _nightly(self, day: date) -> Detail:
        with self.nba() as session:
            schedule = nba_cdn.fetch_schedule(session, self.cache, season_label(day), force=True)
            noted = {g for g in self.finals.get(day, set()) if is_counted(_Game(g, ""))}
            games = sorted(final_games(schedule, day) | noted)
            pending = [game_id for game_id in games if not fully_cached(self.cache, game_id)]
            failed: list[Detail] = []
            for game_id in pending:
                results = [
                    *fetch_game(session, self.cache, game_id),
                    *_fetch_cdn_game(session, self.cache, game_id),
                ]
                failed.extend(
                    {"endpoint": r.endpoint, "key": r.key, "error": r.error}
                    for r in results
                    if r.status is FetchStatus.FAILED
                )
        self.finals.pop(day, None)
        return {"date": day.isoformat(), "games": games, "fetched": pending, "failed": failed}

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

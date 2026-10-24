"""The worker's jobs, on APScheduler: each runs the functions the CLI does and records a `job_run`
row as it starts and finishes (spec §3.3, §6.4).

`nightly` refreshes the schedule, fetches, parses and loads, rebuilds the roster memberships from
rosters, transactions and games, then derives the date (`derive.run`): garbage time, windows,
rotation state, events, flags, local BPM, and a sample of two teams' stints reconciled with
NBA.com's. A game stats.nba.com fails for is marked `stats_failed` in `load_log` and parsed from
cdn.nba.com's live box (spec §4.2); `fetch_missing` re-tries stats.nba.com for those games. A
response whose shape has drifted from its endpoint's (`sources.drift`) is recorded in `load_log`
with status `drift`, and fails the run once the rest of it is done.
`weekly` re-fetches, parses and loads the season's shot charts, then runs the weekly overlays;
`injuries` and `daily_overlays` run theirs (`closinglineup.overlays`).
"""

import json
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from functools import partial
from typing import Any

import httpx
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
from closinglineup.db.models import Game, JobRun, LoadLog, PlayerGame, RunStatus
from closinglineup.derive.reconcile import sample_teams, season_type_of
from closinglineup.derive.roster import rebuild_roster_membership
from closinglineup.derive.run import RECONCILE, DeriveRules, derive, missing_dates
from closinglineup.derive.run import raise_for_failures as raise_for_derive_failures
from closinglineup.heartbeat import Heartbeat, JobFamily
from closinglineup.load.loader import load_dates, load_keyed, raise_for_failures, write_load_log
from closinglineup.load.parquet import ParquetStore
from closinglineup.overlays import OverlayFamily, by_name, overlays_in
from closinglineup.pipeline import SHOT_TABLE, parse_date, parse_shot_charts
from closinglineup.season import Phase, is_counted, season_bounds, season_label
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.drift import SchemaDrift
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

NIGHTLY = "nightly"
FETCH_MISSING = "fetch_missing"
POLL_FINALS = "poll_finals"
WEEKLY = "weekly"
INJURIES = "injuries"
DAILY_OVERLAYS = "daily_overlays"
# `injuries`' last run of the day, the only one whose success pings the daily heartbeat.
LAST_INJURIES_RUN = time(18, 30)

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
type Step = tuple[str, Callable[[], Detail]]

log = logging.getLogger(__name__)


class JobFailedError(Exception):
    """A job that ran every step but some failed; `detail`, with what each step did, is recorded
    beside the error."""

    def __init__(self, message: str, detail: Detail) -> None:
        super().__init__(message)
        self.detail = detail


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


def stats_missing(cache: RawCache, game_id: str) -> list[str]:
    """The stats.nba.com per-game endpoints of `game_id` not yet in the cache, `NEVER_CACHED`
    aside."""
    return [
        endpoint
        for endpoint in nba_stats.GAME_ENDPOINTS
        if endpoint not in NEVER_CACHED and not cache.exists(nba_stats.SOURCE, endpoint, game_id)
    ]


def fully_cached(cache: RawCache, game_id: str) -> bool:
    """Whether every per-game endpoint of `game_id` but `NEVER_CACHED` is in the cache."""
    return not stats_missing(cache, game_id) and all(
        cache.exists(nba_cdn.SOURCE, endpoint, game_id) for endpoint in CDN_GAME_ENDPOINTS
    )


# The `load_log` row of a date's stats.nba.com fetch: failed while any of its games is
# `stats_failed`, with each such game's uncached endpoints in its detail, else succeeded.
STATS_TABLE = "player_game"
STATS_STEP = "fetch_stats"
STATS_FAILED = "stats_failed"


def record_stats_fetch(
    db: sessionmaker[Session],
    day: date,
    stats_failed: dict[str, list[str]],
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Record `day`'s stats.nba.com fetch: `stats_failed` maps each game it failed for to the
    endpoints still not cached."""
    status = RunStatus.FAILED if stats_failed else RunStatus.SUCCEEDED
    with db.begin() as session:
        write_load_log(
            session,
            STATS_TABLE,
            day,
            status,
            started_at,
            finished_at,
            {STATS_FAILED: stats_failed},
            step=STATS_STEP,
        )


def stats_failed_games(db: sessionmaker[Session]) -> dict[date, list[str]]:
    """The `stats_failed` games of every date whose stats.nba.com fetch last failed, by date."""
    with db() as session:
        found = session.execute(
            select(LoadLog.game_date, LoadLog.detail)
            .where(
                LoadLog.table_name == STATS_TABLE,
                LoadLog.step == STATS_STEP,
                LoadLog.status == RunStatus.FAILED,
            )
            .order_by(LoadLog.game_date)
        )
        return {day: sorted((detail or {}).get(STATS_FAILED, {})) for day, detail in found}


# The `load_log` row of a date's responses from one endpoint that drifted: its table is the
# endpoint, as `<source>/<endpoint>`, its status `drift`, and its detail lists each drifted
# response by cache key, with how it differs.
DRIFT_STEP = "fetch"


def record_drift(
    db: sessionmaker[Session],
    day: date,
    drifted: list[EndpointResult],
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Record each endpoint of `drifted`, the DRIFT results of `day`'s fetch, in `load_log`."""
    by_endpoint: dict[str, dict[str, str | None]] = {}
    for result in drifted:
        by_endpoint.setdefault(f"{result.source}/{result.endpoint}", {})[result.key] = result.error
    with db.begin() as session:
        for endpoint, keys in by_endpoint.items():
            write_load_log(
                session,
                endpoint,
                day,
                RunStatus.DRIFT,
                started_at,
                finished_at,
                {"drift": keys},
                step=DRIFT_STEP,
            )


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
    """`fetch_game` for the cdn.nba.com feeds: a feed that answers badly is FAILED, and one that
    has drifted DRIFT, not raised."""
    results: list[EndpointResult] = []
    for endpoint, fetch in CDN_GAME_ENDPOINTS.items():
        cached = not force and cache.exists(nba_cdn.SOURCE, endpoint, game_id)
        try:
            fetch(session, cache, game_id, force=force)
        except NbaHttpError as exc:
            status, error = FetchStatus.FAILED, str(exc)
        except SchemaDrift as exc:
            status, error = FetchStatus.DRIFT, str(exc)
        else:
            status = FetchStatus.CACHED if cached else FetchStatus.FETCHED
            error = None
        results.append(EndpointResult(endpoint, game_id, status, error, source=nba_cdn.SOURCE))
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


# The `EndpointResult.endpoint` of a game's stats.nba.com endpoints skipped once it stopped
# answering.
STATS_UNREACHABLE = "stats.nba.com"


def fetch_final_games(
    session: NbaSession, cache: RawCache, game_ids: list[str]
) -> tuple[list[EndpointResult], str | None]:
    """`fetch_final_game` for each of `game_ids`, and why stats.nba.com stopped answering, if it
    did.

    Once stats.nba.com raises `NbaUnreachableError`, its endpoints are skipped for the rest of the
    games, each game reported FAILED under `STATS_UNREACHABLE`, and only their cdn.nba.com feeds
    are fetched, so the live box stands in for every one. cdn.nba.com not answering still raises.
    """
    results: list[EndpointResult] = []
    unreachable: str | None = None
    for game_id in game_ids:
        if unreachable is None:
            try:
                results += fetch_game(session, cache, game_id)
            except NbaUnreachableError as exc:
                unreachable = str(exc)
        if unreachable is not None:
            failed = EndpointResult(STATS_UNREACHABLE, game_id, FetchStatus.FAILED, unreachable)
            results.append(failed)
        results += _fetch_cdn_game(session, cache, game_id)
    return results, unreachable


@dataclass
class Jobs:
    """Every job, with what they share; the worker schedules them and the CLI runs them once.

    `nba` opens a fresh `NbaSession` for each run, which the run closes; `http` likewise a fresh
    plain httpx client, for the sources outside NBA.com: the injuries fallback's ESPN, darko.app
    and Hoops Rumors.
    """

    settings: Settings
    clock: Clock
    cache: RawCache
    store: ParquetStore
    nba: Callable[[], NbaSession]
    http: Callable[[], httpx.Client]
    db: sessionmaker[Session]
    heartbeat: Heartbeat
    # Games `poll_finals` has seen go final, by game date, until `nightly` has run for the date.
    finals: dict[date, set[str]] = field(default_factory=dict[date, set[str]])

    def nightly(self, day: date) -> Detail:
        """Refresh the schedule, fetch every per-game endpoint of each counted game final on `day`
        that is not yet fully cached, then parse, load and derive the date.

        A game is final if the schedule says so or `poll_finals` saw it go final. An endpoint that
        answers badly is listed in the run's detail and does not fail the job, since
        `playbyplayv2` currently always does (see `nba_stats.fetch_playbyplay_v2`); nor does a
        game a parser cannot read, which `parse_date` leaves out. Each earlier parsed date whose
        last load of a table failed or never ran is loaded again, and the roster memberships are
        rebuilt from the cached rosters, the transactions and every loaded game
        (`derive.roster`). A table that fails to load fails the job, and its failed `load_log` row
        is there for the next run; so does a rebuild that fails.

        Then the date is derived (`derive.run`), after any earlier date of its season whose derive
        has not all succeeded, oldest first; each step's `load_log` row says how it went, and a
        step that fails fails the job, its date derived again on the next run. The derive ends by
        reconciling two teams' stints, a different two each day (`reconcile.sample_teams`), with
        NBA.com's lineups and on/off, re-fetched for the purpose. A row outside tolerance is
        logged as a warning and listed in the detail; it does not fail the job, nor does a
        response that failed to fetch, which skips the reconciliation.

        A game with a stats.nba.com endpoint still not cached after the fetch, `playbyplayv2`
        aside, is `stats_failed`: the date's `fetch_stats` row of `load_log` lists it, and it is
        logged as a warning and listed under `warnings`, without failing the job. Its box score
        is parsed from cdn.nba.com's live box when stats.nba.com's traditional box is not cached
        (`pipeline`). Once stats.nba.com stops answering altogether, the rest of the date's games
        are fetched from cdn.nba.com alone and reconciliation is skipped (`fetch_final_games`).
        `fetch_missing` re-tries them.

        A response whose shape has drifted from its endpoint's is not cached (`sources.drift`), so
        a game's drifted stats.nba.com endpoint leaves it `stats_failed`, as one that failed does;
        a drifted schedule gives way to the copy cached before it, and with none the run fails.
        Each endpoint that drifted has a `load_log` row of the date with status `drift`
        (`record_drift`), and once the run is done it fails with every difference in its error,
        which the heartbeat's `/fail` carries.
        """
        return self._run(NIGHTLY, lambda: self._nightly(day), family="nightly")

    def fetch_missing(self) -> Detail:
        """Re-try stats.nba.com for every `stats_failed` game a past `nightly` recorded, then parse
        and load each of their dates again, so a game whose stats.nba.com box is now cached has
        its cdn.nba.com rows replaced. Each date's `fetch_stats` row then lists the games still
        failing, or none. If stats.nba.com stops answering, the games not yet re-tried stay
        `stats_failed`, and the detail's `unreachable` says why.

        The dates are not derived again: their minutes, starters and DNP reasons are the same
        from either source, and the next `nightly` refreshes the windows and BPM over the season.
        A table that fails to load fails the job, as in `nightly`, and leaves every game
        `stats_failed`, for the next run to load again.
        """
        return self._run(FETCH_MISSING, self._fetch_missing)

    def nightly_yesterday(self) -> Detail:
        """`nightly` for yesterday in `TZ`, as the worker runs it at 03:30."""
        today = self._local_now().date()
        return self.nightly(today - timedelta(days=1))

    def weekly(self) -> Detail:
        """Re-fetch the shot chart of every player who has played this season, in each season
        type he has played in, then parse and load them (spec §3.3); then run the weekly
        overlays. Each of those steps runs whether or not the one before it failed, and any that
        failed fails the job.

        A shot chart that fails to fetch or parse is listed in the step's detail and does not fail
        it; whatever was loaded from it before stays. Every shot chart failing to fetch fails the
        step, as does a shot file that fails to load: one with a shot in a game that has no
        `game` row yet, for instance.
        """
        return self._run(WEEKLY, self._weekly, family="weekly")

    def injuries(self, day: date, *, last: bool = True) -> Detail:
        """Run the `injuries` overlays for `day`, each in isolation; any that failed fails the job
        and pings the daily heartbeat's `/fail`. Only the day's `last` run pings it on success,
        so the check fails if the day's reports never landed."""
        return self._run(
            INJURIES,
            lambda: self._overlays("injuries", day),
            family="daily",
            ping_success=last,
        )

    def injuries_now(self) -> Detail:
        """`injuries` for today in `TZ`, as the worker runs it; the 18:30 run is the last."""
        now = self._local_now()
        return self.injuries(now.date(), last=now.time() >= LAST_INJURIES_RUN)

    def daily_overlays(self, day: date) -> Detail:
        """Run the `daily` overlays for `day`, each in isolation; any that failed fails the job."""
        return self._run(DAILY_OVERLAYS, lambda: self._overlays("daily", day), family="daily")

    def daily_overlays_today(self) -> Detail:
        """`daily_overlays` for today in `TZ`, as the worker runs it at 09:00."""
        return self.daily_overlays(self._local_now().date())

    def overlay(self, name: str, day: date) -> Detail:
        """Run the overlay registered as `name` once, for `day`, with no heartbeat."""
        overlay = by_name(name)
        return self._run(f"overlay:{name}", lambda: overlay.run(self, day))

    def poll_finals(self) -> Detail:
        """Note the games today's scoreboard shows final, for `nightly` to fetch."""
        return self._run(POLL_FINALS, self._poll_finals)

    def _nightly(self, day: date) -> Detail:
        started_at = self.clock.now()
        drifted: list[EndpointResult] = []
        with self.nba() as session:
            season = season_label(day)
            try:
                schedule = nba_cdn.fetch_schedule(session, self.cache, season, force=True)
            except SchemaDrift as exc:
                drifted.append(
                    EndpointResult(
                        nba_cdn.SCHEDULE, season, FetchStatus.DRIFT, str(exc), nba_cdn.SOURCE
                    )
                )
                # Recorded at once: with no copy cached before it, the run stops here.
                record_drift(self.db, day, drifted, started_at, self.clock.now())
                if (cached := self.cache.get(nba_cdn.SOURCE, nba_cdn.SCHEDULE, season)) is None:
                    raise
                schedule = json.loads(cached)
            noted = {g for g in self.finals.get(day, set()) if is_counted(_Game(g, ""))}
            games = sorted(final_games(schedule, day) | noted)
            pending = [game_id for game_id in games if not fully_cached(self.cache, game_id)]
            results, unreachable = fetch_final_games(session, self.cache, pending)
            # The season type of the date's games; None for a date with none counted in one.
            season_type = season_type_of(games[-1]) if games else None
            teams = sample_teams(day)
            refresh: list[EndpointResult] = []
            if season_type is not None and unreachable is not None:
                # stats.nba.com has already used up its retries; asking again would only wait.
                refresh = [
                    EndpointResult("reconcile", day.isoformat(), FetchStatus.FAILED, unreachable)
                ]
            elif season_type is not None:
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
        drifted += [r for r in results if r.status is FetchStatus.DRIFT]
        record_drift(self.db, day, drifted, started_at, self.clock.now())
        for result in drifted:
            log.warning("schema drift: %s", result.error, extra={"date": day.isoformat()})
        stats_failed = {g: missing for g in games if (missing := stats_missing(self.cache, g))}
        record_stats_fetch(self.db, day, stats_failed, started_at, self.clock.now())
        parsed = parse_date(self.cache, self.store, day, self.settings.tz)
        warnings = [
            f"{game_id}: stats.nba.com failed for {', '.join(missing)}"
            + ("; box score from cdn.nba.com" if game_id in parsed.fallback else "")
            for game_id, missing in stats_failed.items()
        ]
        for warning in warnings:
            log.warning("stats_failed: %s", warning, extra={"date": day.isoformat()})
        loaded = load_dates(self.db, self.store, self.clock, [day])
        # Self-heals any earlier date a past run left without a successful load (spec §3.2).
        earlier = [d for d in self.store.dates() if d != day]
        healed = load_dates(self.db, self.store, self.clock, earlier, missing=True)
        spans = rebuild_roster_membership(self.db, self.cache, self.settings.tz)
        raise_for_failures([*loaded, *healed, spans])
        # Any earlier date of the season a past run left underived goes first: a date's events
        # compare with the team's previous game date's rotation state.
        season_start, _ = season_bounds(season_label(day))
        days = [*missing_dates(self.db, season_start, day - timedelta(days=1)), day]
        stale = next((r for r in refresh if r.status is FetchStatus.FAILED), None)
        derived = derive(
            self.db,
            self.cache,
            self.clock,
            days,
            DeriveRules.from_settings(self.settings),
            season_type=season_type,
            reconcile_skip=f"{stale.endpoint} {stale.key} failed to refresh" if stale else None,
        )
        raise_for_derive_failures(derived)
        detail: Detail = {
            "date": day.isoformat(),
            "games": games,
            "fetched": pending,
            "failed": failed,
            "drift": [r.error for r in drifted],
            "stats_failed": sorted(stats_failed),
            "warnings": warnings,
            "parsed": parsed.parsed,
            "parse_fallback": parsed.fallback,
            "parse_failed": parsed.failed,
            "parse_missing": parsed.missing,
            "loaded": {result.table: result.rows for result in loaded},
            "healed": [f"{result.table} {result.key}" for result in healed],
            "memberships": spans.rows,
            "derived": [d.isoformat() for d in days],
            "derive": {r.step.name: r.detail for r in derived if r.day == day},
            "reconcile": next((r.detail for r in derived if r.step == RECONCILE), None),
        }
        if drifted:
            listed = "; ".join(str(r.error) for r in drifted)
            raise JobFailedError(f"{len(drifted)} responses drifted: {listed}", detail)
        return detail

    def _fetch_missing(self) -> Detail:
        started_at = self.clock.now()
        pending = stats_failed_games(self.db)
        unreachable: str | None = None
        with self.nba() as session:
            try:
                for games in pending.values():
                    for game_id in games:
                        fetch_game(session, self.cache, game_id, skip=NEVER_CACHED)
            except NbaUnreachableError as exc:
                # The games not yet re-tried stay `stats_failed` for the next run.
                unreachable = str(exc)
        days = sorted(pending)
        parsed = [parse_date(self.cache, self.store, day, self.settings.tz) for day in days]
        loaded = load_dates(self.db, self.store, self.clock, days)
        # Only once the rows are loaded do the games stop being `stats_failed`.
        raise_for_failures(loaded)
        still: dict[str, dict[str, list[str]]] = {}
        for day, games in pending.items():
            failing = {g: missing for g in games if (missing := stats_missing(self.cache, g))}
            record_stats_fetch(self.db, day, failing, started_at, self.clock.now())
            if failing:
                still[day.isoformat()] = failing
        return {
            "dates": [day.isoformat() for day in days],
            "games": sorted(g for games in pending.values() for g in games),
            "still_failed": {day: dict(sorted(failing.items())) for day, failing in still.items()},
            "unreachable": unreachable,
            "fallback": sorted(g for report in parsed for g in report.fallback),
            "parse_failed": {g: e for report in parsed for g, e in report.failed.items()},
        }

    def _local_now(self) -> datetime:
        return self.clock.now().astimezone(self.settings.tz)

    def _overlay_steps(self, family: OverlayFamily, day: date) -> list[Step]:
        return [(overlay.name, partial(overlay.run, self, day)) for overlay in overlays_in(family)]

    def _overlays(self, family: OverlayFamily, day: date) -> Detail:
        return self._steps(self._overlay_steps(family, day), {"date": day.isoformat()})

    def _steps(self, steps: list[Step], detail: Detail) -> Detail:
        """Run each step in turn, in isolation: one that raises is logged and listed under
        `failed`, and the rest still run. Any that failed fails the job (`JobFailedError`)."""
        ran: dict[str, Detail] = {}
        failed: dict[str, str] = {}
        for name, step in steps:
            try:
                ran[name] = step()
            except Exception as exc:
                log.exception("job step failed", extra={"step": name})
                failed[name] = f"{type(exc).__name__}: {exc}"
        detail = {**detail, "ran": ran, "failed": failed}
        if failed:
            summary = "; ".join(f"{name}: {error}" for name, error in failed.items())
            raise JobFailedError(f"{len(failed)} of {len(steps)} failed: {summary}", detail)
        return detail

    def _weekly(self) -> Detail:
        # One reading of the clock, so the shots and the overlays agree on the date.
        today = self._local_now().date()
        shots = partial(self._shots, season_label(today))
        return self._steps([("shots", shots), *self._overlay_steps("weekly", today)], {})

    def _shots(self, season: str) -> Detail:
        players = shot_chart_players(self.db, season)
        with self.nba() as session:
            results = [
                result
                for player_id, season_type in players
                for result in fetch_player_shots(
                    session, self.cache, player_id, season, season_type, force=True
                )
            ]
        # A shot chart that drifted is not cached, and parsing it would read last week's copy.
        missed = (FetchStatus.FAILED, FetchStatus.DRIFT)
        fetched = [r.key for r in results if r.status not in missed]
        if results and not fetched:
            raise NbaHttpError(f"every one of {len(results)} shot charts failed to fetch")
        parsed = parse_shot_charts(self.cache, self.store, fetched)
        loaded = load_keyed(self.db, self.store, self.clock, SHOT_TABLE, parsed.rows)
        raise_for_failures(loaded)
        return {
            "season": season,
            "shot_charts": len(players),
            "fetched": len(fetched),
            "failed": [{"key": r.key, "error": r.error} for r in results if r.status in missed],
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

    def _run(
        self,
        job: str,
        body: Callable[[], Detail],
        family: JobFamily | None = None,
        *,
        ping_success: bool = True,
    ) -> Detail:
        """Run `body` between its `job_run` row's start and finish, and ping `family`'s heartbeat,
        on success only if `ping_success`.

        Anything `body` raises marks the run failed, pings `/fail` with the exception summary, and
        is re-raised, so APScheduler logs it and the CLI exits 1. A `JobFailedError`'s detail is
        recorded beside the error.
        """
        run_id: int | None = None
        try:
            run_id = self._start(job)
            detail = body()
            self._finish(run_id, RunStatus.SUCCEEDED, detail)
        except Exception as exc:
            failed = exc.detail if isinstance(exc, JobFailedError) else {}
            reason = str(exc) if isinstance(exc, JobFailedError) else f"{type(exc).__name__}: {exc}"
            if family is not None:
                self.heartbeat.ping(family, fail=True, reason=reason)
            if run_id is not None:
                self._record_failure(job, run_id, {"error": reason, **failed})
            raise
        if family is not None and ping_success:
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

    def _record_failure(self, job: str, run_id: int, detail: Detail) -> None:
        """Mark the run failed; a database that is down too must not hide the job's own error."""
        try:
            self._finish(run_id, RunStatus.FAILED, detail)
        except SQLAlchemyError:
            log.exception("could not record failed job run", extra={"job": job, "run_id": run_id})


def nightly_trigger(tz: tzinfo) -> CronTrigger:
    return CronTrigger(hour=3, minute=30, timezone=tz)


def weekly_trigger(tz: tzinfo) -> CronTrigger:
    return CronTrigger(day_of_week="mon", hour=4, minute=30, timezone=tz)


def injuries_trigger(tz: tzinfo) -> OrTrigger:
    """10:00, 13:00, 16:00 and 18:30."""
    return OrTrigger(
        [
            CronTrigger(hour="10,13,16", minute=0, timezone=tz),
            CronTrigger(hour=LAST_INJURIES_RUN.hour, minute=LAST_INJURIES_RUN.minute, timezone=tz),
        ]
    )


def daily_overlays_trigger(tz: tzinfo) -> CronTrigger:
    return CronTrigger(hour=9, minute=0, timezone=tz)


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
        (INJURIES, jobs.injuries_now, injuries_trigger(tz)),
        (DAILY_OVERLAYS, jobs.daily_overlays_today, daily_overlays_trigger(tz)),
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

"""Command-line entry point: every job is a command (spec §3.1)."""

import logging
import signal
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import httpx
import typer
import uvicorn

from closinglineup.backfill import fetch_season
from closinglineup.clock import POLL_SECONDS, Clock, SystemClock
from closinglineup.db.models import RunStatus
from closinglineup.db.schema import SchemaNotAtHeadError, wait_for_schema_head
from closinglineup.db.session import make_engine, make_session_factory
from closinglineup.derive.reconcile import (
    LINEUP_TOLERANCE_POSSESSIONS,
    MIN_POSSESSIONS,
    ON_COURT_TOLERANCE,
    MissingResponseError,
    ReconcileError,
    describe,
    reconcile,
)
from closinglineup.derive.roster import rebuild_roster_membership
from closinglineup.derive.run import DeriveRules, StepResult, missing_dates
from closinglineup.derive.run import derive as run_derive
from closinglineup.health import DRIFT_HOURS, STALE_HOURS, unhealthy_reason
from closinglineup.heartbeat import Heartbeat
from closinglineup.load.loader import LoadResult, load_dates, load_keyed
from closinglineup.load.parquet import ROW_MODELS, ParquetStore
from closinglineup.logging import configure_logging
from closinglineup.overlays import UnknownOverlayError, by_name
from closinglineup.pipeline import (
    SHOT_TABLE,
    NotCachedError,
    ParseReport,
    game_date_of,
    parse_date,
    parse_shot_charts,
    shot_chart_keys,
)
from closinglineup.reparse import UncachedError, reparse
from closinglineup.scheduler import Jobs, build_scheduler
from closinglineup.season import season_label
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaUnreachableError, make_nba_session
from closinglineup.sources.nba_stats import (
    FetchStatus,
    SeasonType,
    fetch_aggregates,
    fetch_game,
    fetch_player_shots,
    fetch_reconciliation,
    fetch_rosters,
)
from closinglineup.sources.web import make_client
from closinglineup.teams import TEAMS, UnknownTeam, by_tricode

app = typer.Typer(no_args_is_help=True, add_completion=False)

log = logging.getLogger(__name__)

COMMANDS = {
    "fetch": "Fetch raw responses from NBA.com into the raw cache.",
    "parse": "Parse cached raw responses into parquet rows.",
    "load": "Load parquet rows into Postgres.",
    "derive": "Flag garbage time, write BPM, Win Shares and VORP, refresh windows, write rotation "
    "state, events and flags, and reconcile a sample.",
    "overlays": "Run one overlay by name: injuries, transactions, impact metrics or contracts.",
    "api": "Serve the API and the built frontend.",
    "worker": "Run the job scheduler.",
    "nightly": "Run the nightly job once: refresh the schedule, fetch, parse, load and derive a "
    "date.",
    "weekly": "Run the weekly job once: re-fetch, parse and load the season's shot charts.",
    "health": f"Check that the database answers, no response has drifted in the last {DRIFT_HOURS} "
    f"h, and the latest derived date is within {STALE_HOURS} h of the latest final game.",
    "reparse": "Parse, load and derive a run of dates again from the raw cache alone, after a "
    "parser change.",
    "reconcile": "Compare lineup and on-court ratings from stints with NBA.com's.",
}


@app.command("health", help=COMMANDS["health"])
def health() -> None:
    engine = make_engine(Settings())
    try:
        reason = unhealthy_reason(engine)
    finally:
        engine.dispose()
    if reason is not None:
        typer.echo(f"health: {reason}", err=True)
        raise typer.Exit(1)
    typer.echo("health: ok")


# Plain assignments, not `type` aliases, which typer cannot read.
DateOption = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], help="A game date.")]
FromOption = Annotated[
    datetime | None, typer.Option("--from", formats=["%Y-%m-%d"], help="First game date.")
]
ToOption = Annotated[
    datetime | None, typer.Option("--to", formats=["%Y-%m-%d"], help="Last game date.")
]


@app.command("fetch", help=COMMANDS["fetch"])
def fetch(
    game_id: Annotated[
        str | None, typer.Option(help="Fetch every per-game endpoint for this game.")
    ] = None,
    rosters: Annotated[
        bool, typer.Option("--rosters", help="Fetch every team's roster and every player.")
    ] = False,
    shots: Annotated[bool, typer.Option("--shots", help="Fetch --player-id's shot chart.")] = False,
    aggregates: Annotated[
        bool,
        typer.Option(
            "--aggregates",
            help="Fetch the league-wide game logs, lineups, stats dashboards and on/off.",
        ),
    ] = False,
    player_id: Annotated[int | None, typer.Option(help="The player whose shots to fetch.")] = None,
    season: Annotated[
        str | None,
        typer.Option(
            help="Season, e.g. 2026-27. Defaults to the current one. Alone, fetch every final "
            "game of the season not yet cached, oldest first."
        ),
    ] = None,
    start: FromOption = None,
    end: ToOption = None,
    season_type: Annotated[SeasonType, typer.Option(help="Season type.")] = (
        SeasonType.REGULAR_SEASON
    ),
    missing: Annotated[
        bool,
        typer.Option(
            "--missing",
            help="Re-try stats.nba.com for every game a past nightly fell back to cdn.nba.com "
            "for, then parse and load their dates again.",
        ),
    ] = False,
    force: Annotated[bool, typer.Option(help="Re-fetch responses already cached.")] = False,
) -> None:
    modes = sum([game_id is not None, rosters, shots, aggregates, missing])
    walk = modes == 0 and season is not None
    if modes != 1 and not walk:
        raise typer.BadParameter(
            "pass exactly one of --game-id, --rosters, --shots, --aggregates, --missing, "
            "or --season alone"
        )
    if missing and (season is not None or force):
        raise typer.BadParameter("--missing takes no --season and no --force")
    if shots and player_id is None:
        raise typer.BadParameter("--shots needs --player-id")
    if (start is not None or end is not None) and not walk:
        raise typer.BadParameter("--from and --to go with --season alone")
    if start is not None and end is not None and start > end:
        raise typer.BadParameter("--from is after --to")
    settings = Settings()
    clock = SystemClock()
    if missing:
        _fetch_missing(settings, clock)
        return
    cache = RawCache(settings.data_dir, clock)
    season = season or season_label(clock.now().astimezone(settings.tz).date())
    if walk:
        first = start.date() if start else None
        last = end.date() if end else None
        _fetch_season(settings, clock, cache, season, first, last, force=force)
        return
    with make_nba_session(settings, clock) as session:
        try:
            if game_id is not None:
                results = fetch_game(session, cache, game_id, force=force)
            elif rosters:
                results = fetch_rosters(session, cache, season, force=force)
            elif shots and player_id is not None:
                results = fetch_player_shots(
                    session, cache, player_id, season, season_type, force=force
                )
            else:
                results = fetch_aggregates(session, cache, season, season_type, force=force)
        except NbaUnreachableError as exc:
            typer.echo(f"fetch: stopped: {exc}")
            raise typer.Exit(1) from None
    for result in results:
        reason = f": {result.error}" if result.error else ""
        typer.echo(f"{result.endpoint} {result.key}: {result.status}{reason}")
    if any(result.status in (FetchStatus.FAILED, FetchStatus.DRIFT) for result in results):
        raise typer.Exit(1)


def _fetch_missing(settings: Settings, clock: Clock) -> None:
    """`fetch --missing`: a line per game stats.nba.com still fails for."""
    with _open_jobs(settings, clock) as jobs:
        try:
            detail = jobs.fetch_missing()
        except Exception as exc:
            typer.echo(f"fetch --missing: failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(1) from None
    still = [
        (day, game_id, endpoints)
        for day, games in detail["still_failed"].items()
        for game_id, endpoints in games.items()
    ]
    for day, game_id, endpoints in still:
        typer.echo(f"{day} {game_id}: stats.nba.com failed again for {', '.join(endpoints)}")
    for failed_game, error in detail["parse_failed"].items():
        typer.echo(f"parse {failed_game}: failed: {error}")
    if detail["unreachable"] is not None:
        typer.echo(f"fetch --missing: stopped: {detail['unreachable']}")
    typer.echo(
        f"fetch --missing: {len(detail['games'])} stats_failed games, "
        f"{len(detail['games']) - len(still)} fetched, {len(still)} still failing"
    )
    if still:
        raise typer.Exit(1)


def _days(date: datetime | None, start: datetime | None, end: datetime | None) -> list[Date] | None:
    """The game dates `--date` or `--from`/`--to` name, or None if neither was passed."""
    if date is not None:
        if start is not None or end is not None:
            raise typer.BadParameter("pass --date or --from/--to, not both")
        return [date.date()]
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise typer.BadParameter("--from and --to go together")
    if start > end:
        raise typer.BadParameter("--from is after --to")
    return [start.date() + timedelta(days=n) for n in range((end - start).days + 1)]


def _fetch_season(
    settings: Settings,
    clock: Clock,
    cache: RawCache,
    season: str,
    start: Date | None,
    end: Date | None,
    *,
    force: bool,
) -> None:
    """`fetch --season`: a line per game as it is done, so a long run shows its progress."""
    games = cached = 0
    failed: list[str] = []

    def paused(day: Date, game_id: str, wait: timedelta) -> None:
        minutes = int(wait.total_seconds() // 60)
        typer.echo(f"{day} {game_id}: no answer from NBA.com, trying again in {minutes} min")

    with make_nba_session(settings, clock) as session:
        try:
            for game in fetch_season(
                session, cache, clock, season, start=start, end=end, force=force, on_pause=paused
            ):
                games += 1
                if not game.results:
                    cached += 1
                    continue
                for result in game.failed:
                    typer.echo(f"{result.endpoint} {result.key}: failed: {result.error}")
                outcome = f"{len(game.failed)} failed" if game.failed else "fetched"
                typer.echo(f"{game.game_date} {game.game_id}: {outcome}")
                if game.failed:
                    failed.append(game.game_id)
        except NbaHttpError as exc:
            typer.echo(f"fetch {season}: failed: {exc}")
            raise typer.Exit(1) from None
        except NbaUnreachableError as exc:
            typer.echo(f"fetch {season}: stopped: {exc}")
            typer.echo(f"fetch {season}: run the same command again to resume")
            raise typer.Exit(1) from None
    typer.echo(
        f"fetch {season}: {games} final games, {cached} already cached, "
        f"{games - cached} fetched, {len(failed)} with failures"
    )
    if failed:
        raise typer.Exit(1)


@app.command("parse", help=COMMANDS["parse"])
def parse(
    date: DateOption = None,
    start: FromOption = None,
    end: ToOption = None,
    game_id: Annotated[
        str | None,
        typer.Option(help="Parse this game's date: a date's parquet files hold it whole."),
    ] = None,
    shots: Annotated[
        bool, typer.Option("--shots", help="Parse every cached shot chart of --season.")
    ] = False,
    season: Annotated[
        str | None, typer.Option(help="With --shots: e.g. 2026-27. Defaults to the current one.")
    ] = None,
) -> None:
    settings = Settings()
    cache = RawCache(settings.data_dir)
    store = ParquetStore(settings.data_dir)
    days = _days(date, start, end)
    if sum([days is not None, game_id is not None, shots]) != 1:
        raise typer.BadParameter("pass exactly one of --date, --from/--to, --game-id, --shots")
    if season is not None and not shots:
        raise typer.BadParameter("--season goes with --shots")
    if shots:
        season = season or season_label(SystemClock().now().astimezone(settings.tz).date())
        report = parse_shot_charts(cache, store, shot_chart_keys(cache, season))
        for key, error in report.failed.items():
            typer.echo(f"parse shots {key}: failed: {error}")
        typer.echo(f"parse shots {season}: {len(report.rows)} shot charts parsed")
        if report.failed:
            raise typer.Exit(1)
        return
    failed = False
    if game_id is not None:
        try:
            days = [game_date_of(cache, game_id)]
        except NotCachedError as exc:
            typer.echo(f"parse {game_id}: failed: {exc}")
            raise typer.Exit(1) from None
    for day in days or []:
        try:
            report = parse_date(cache, store, day, settings.tz)
        except NotCachedError as exc:
            typer.echo(f"parse {day}: failed: {exc}")
            failed = True
            continue
        _echo_parse(report)
        failed = failed or bool(report.failed)
    if failed:
        raise typer.Exit(1)


def _echo_parse(report: ParseReport) -> None:
    day = report.game_date
    for failed_game, error in report.failed.items():
        typer.echo(f"parse {day} {failed_game}: failed: {error}")
    for missing_game in report.missing:
        typer.echo(f"parse {day} {missing_game}: final, but no box score cached")
    typer.echo(f"parse {day}: {len(report.parsed)} games parsed")


@app.command("load", help=COMMANDS["load"])
def load(
    date: DateOption = None,
    start: FromOption = None,
    end: ToOption = None,
    missing: Annotated[
        bool,
        typer.Option(
            "--missing",
            help="Only the tables and dates not yet loaded; with no dates, of every parsed date.",
        ),
    ] = False,
    shots: Annotated[bool, typer.Option("--shots", help="Load every parsed shot chart.")] = False,
    memberships: Annotated[
        bool,
        typer.Option(
            "--memberships",
            help="Rebuild the roster memberships from rosters, transactions and loaded games.",
        ),
    ] = False,
) -> None:
    days = _days(date, start, end)
    if shots and memberships:
        raise typer.BadParameter("pass --shots or --memberships, not both")
    if (shots or memberships) and (days is not None or missing):
        raise typer.BadParameter("--shots and --memberships take no dates and no --missing")
    if days is None and not missing and not shots and not memberships:
        raise typer.BadParameter("pass --date, --from/--to, --missing, --shots or --memberships")
    settings = Settings()
    engine = make_engine(settings)
    db = make_session_factory(engine)
    store = ParquetStore(settings.data_dir)
    try:
        if memberships:
            cache = RawCache(settings.data_dir)
            results: list[LoadResult] = [rebuild_roster_membership(db, cache, settings.tz)]
        elif shots:
            results = load_keyed(db, store, SystemClock(), SHOT_TABLE)
        else:
            results = load_dates(db, store, SystemClock(), days, missing=missing)
    finally:
        engine.dispose()
    for result in results:
        if result.status is RunStatus.FAILED:
            typer.echo(f"load {result.table} {result.key}: failed: {result.error}")
        else:
            outcome = f"{result.rows} rows, {result.changed} changed, {result.removed} removed"
            typer.echo(f"load {result.table} {result.key}: {result.status}, {outcome}")
    if not results:
        typer.echo("load: nothing to load")
    if any(result.status is RunStatus.FAILED for result in results):
        raise typer.Exit(1)


def _summary(detail: dict[str, Any]) -> str:
    """A step's scalar results as `name value` pairs, e.g. `players 412, changed 3`."""
    return ", ".join(
        f"{name} {value}"
        for name, value in detail.items()
        if isinstance(value, int | float | str) and name not in {"error", "skipped"}
    )


@app.command("derive", help=COMMANDS["derive"])
def derive(
    date: DateOption = None,
    start: FromOption = None,
    end: ToOption = None,
    missing: Annotated[
        bool,
        typer.Option(
            "--missing",
            help="Only the loaded dates whose derive has not all succeeded; with no dates, of "
            "every loaded date.",
        ),
    ] = False,
) -> None:
    days = _days(date, start, end)
    if days is None and not missing:
        raise typer.BadParameter("pass --date, --from/--to or --missing")
    settings = Settings()
    engine = make_engine(settings)
    try:
        db = make_session_factory(engine)
        if missing:
            days = missing_dates(db, days[0], days[-1]) if days else missing_dates(db)
        results = run_derive(
            db,
            RawCache(settings.data_dir),
            SystemClock(),
            days or [],
            DeriveRules.from_settings(settings),
        )
    finally:
        engine.dispose()
    _echo_derive(results)
    if not results:
        typer.echo("derive: nothing to derive")
    if any(result.status is RunStatus.FAILED for result in results):
        raise typer.Exit(1)


def _echo_derive(results: list[StepResult]) -> None:
    for result in results:
        label = f"derive {result.day} {result.step.name}"
        if result.status is RunStatus.FAILED:
            typer.echo(f"{label}: failed: {result.detail['error']}")
            continue
        if "skipped" in result.detail:
            typer.echo(f"{label}: skipped: {result.detail['skipped']}")
            continue
        typer.echo(f"{label}: {result.status}, {_summary(result.detail)}")
        for line in result.detail.get("outside", []):
            typer.echo(f"{label}: outside tolerance: {line}")


@app.command("reparse", help=COMMANDS["reparse"])
def reparse_command(
    start: FromOption = None,
    end: ToOption = None,
    tables: Annotated[
        str | None,
        typer.Option(
            help="Comma-separated tables to parse and load, e.g. player_game,stint. Defaults to "
            "every one; the dates are derived either way."
        ),
    ] = None,
) -> None:
    days = _days(None, start, end)
    if days is None:
        raise typer.BadParameter("pass --from and --to")
    chosen: list[str] | None = None
    if tables is not None:
        chosen = [name.strip() for name in tables.split(",")]
        if unknown := sorted(set(chosen) - set(ROW_MODELS)):
            raise typer.BadParameter(
                f"unknown tables {', '.join(unknown)}; pick from {', '.join(ROW_MODELS)}"
            )
    settings = Settings()
    clock = SystemClock()
    engine = make_engine(settings)
    try:
        report = reparse(
            make_session_factory(engine),
            RawCache(settings.data_dir, clock),
            ParquetStore(settings.data_dir),
            clock,
            days,
            DeriveRules.from_settings(settings),
            settings.tz,
            chosen,
        )
    except UncachedError as exc:
        for day, games in exc.missing.items():
            for game_id, lacks in games.items():
                typer.echo(f"reparse {day} {game_id}: not in the raw cache: {'; '.join(lacks)}")
        typer.echo(f"reparse: refused: {exc}; fetch them first")
        raise typer.Exit(1) from None
    except NotCachedError as exc:
        typer.echo(f"reparse: refused: {exc}")
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    for parsed in report.parsed:
        _echo_parse(parsed)
    loads = [*report.loaded, report.memberships]
    for result in loads:
        if result.status is RunStatus.FAILED:
            typer.echo(f"load {result.table} {result.key}: failed: {result.error}")
    _echo_derive(report.derived)
    load_failed = any(result.status is RunStatus.FAILED for result in loads)
    if load_failed:
        typer.echo("reparse: not derived, since a load failed")
    games = sum(len(parsed.parsed) for parsed in report.parsed)
    typer.echo(
        f"reparse {days[0]} to {days[-1]}: {len(days)} dates, {games} games parsed, "
        f"{sum(result.changed for result in report.loaded)} rows changed"
    )
    if (
        load_failed
        or any(parsed.failed for parsed in report.parsed)
        or any(result.status is RunStatus.FAILED for result in report.derived)
    ):
        raise typer.Exit(1)


def _team_ids(teams: str | None) -> list[int]:
    """The NBA team IDs of comma-separated tricodes, or of every team if None."""
    if teams is None:
        return [team.nba_team_id for team in TEAMS]
    try:
        return [by_tricode(code.strip().upper()).nba_team_id for code in teams.split(",")]
    except UnknownTeam as exc:
        raise typer.BadParameter(str(exc)) from None


@app.command("reconcile", help=COMMANDS["reconcile"])
def reconcile_command(
    season: Annotated[
        str | None, typer.Option(help="Season, e.g. 2025-26. Defaults to the current one.")
    ] = None,
    teams: Annotated[
        str | None, typer.Option(help="Tricodes to reconcile, e.g. BOS,DEN. Defaults to all 30.")
    ] = None,
    season_type: Annotated[SeasonType, typer.Option(help="Season type.")] = (
        SeasonType.REGULAR_SEASON
    ),
    min_possessions: Annotated[
        int, typer.Option(help="Compare only rows NBA.com credits with this many possessions.")
    ] = MIN_POSSESSIONS,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-fetch NBA.com's lineups and on/off first.")
    ] = False,
    show_all: Annotated[
        bool, typer.Option("--all", help="List every row compared, not only those outside.")
    ] = False,
) -> None:
    team_ids = _team_ids(teams)
    settings = Settings()
    clock = SystemClock()
    cache = RawCache(settings.data_dir, clock)
    season = season or season_label(clock.now().astimezone(settings.tz).date())
    if refresh:
        with make_nba_session(settings, clock) as session:
            try:
                results = fetch_reconciliation(
                    session, cache, season, season_type, team_ids, force=True
                )
            except NbaUnreachableError as exc:
                typer.echo(f"reconcile: stopped: {exc}")
                raise typer.Exit(1) from None
        for result in results:
            if result.status in (FetchStatus.FAILED, FetchStatus.DRIFT):
                typer.echo(f"{result.endpoint} {result.key}: {result.status}: {result.error}")
    engine = make_engine(settings)
    try:
        report = reconcile(
            make_session_factory(engine),
            cache,
            season,
            season_type,
            team_ids,
            min_possessions=min_possessions,
        )
    except MissingResponseError as exc:
        typer.echo(f"reconcile: {exc}; run it with --refresh, or fetch --aggregates")
        raise typer.Exit(1) from None
    except ReconcileError as exc:
        typer.echo(f"reconcile: {exc}")
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    for check in [*report.lineups, *report.on_court] if show_all else report.outside:
        mark = "ok" if check.within else "OUTSIDE"
        typer.echo(f"{mark} {describe(check)}")
    outside_lineups = sum(not check.within for check in report.lineups)
    outside_on_court = sum(not check.within for check in report.on_court)
    typer.echo(
        f"reconcile {season} {season_type.slug}: "
        f"{len(report.lineups)} lineups, {outside_lineups} outside "
        f"{LINEUP_TOLERANCE_POSSESSIONS} possessions' worth; "
        f"{len(report.on_court)} players on court, {outside_on_court} outside "
        f"±{ON_COURT_TOLERANCE}"
    )
    if report.outside:
        raise typer.Exit(1)


@contextmanager
def _open_jobs(settings: Settings, clock: Clock) -> Generator[Jobs]:
    engine = make_engine(settings)
    try:
        with httpx.Client() as http:
            yield Jobs(
                settings=settings,
                clock=clock,
                cache=RawCache(settings.data_dir, clock),
                store=ParquetStore(settings.data_dir),
                nba=lambda: make_nba_session(settings, clock),
                http=make_client,
                db=make_session_factory(engine),
                heartbeat=Heartbeat.from_settings(settings, http),
            )
    finally:
        engine.dispose()


@app.command("nightly", help=COMMANDS["nightly"])
def nightly(
    date: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="The game date. Defaults to yesterday in TZ."),
    ] = None,
) -> None:
    settings = Settings()
    clock = SystemClock()
    day = date.date() if date else clock.now().astimezone(settings.tz).date() - timedelta(days=1)
    with _open_jobs(settings, clock) as jobs:
        try:
            detail = jobs.nightly(day)
        except Exception as exc:
            typer.echo(f"nightly {day}: failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(1) from None
    for failure in detail["failed"]:
        typer.echo(f"{failure['endpoint']} {failure['key']}: failed: {failure['error']}")
    for caught in detail["caught_up"]:
        endpoints = ", ".join(caught["endpoints"])
        typer.echo(f"caught up {caught['game_id']} ({caught['date']}): {endpoints}")
    for failed_game, error in detail["parse_failed"].items():
        typer.echo(f"parse {failed_game}: failed: {error}")
    for warning in detail["warnings"]:
        typer.echo(f"warning: {warning}")
    reconciled = detail["reconcile"]
    if reconciled is not None:
        if "skipped" in reconciled:
            typer.echo(f"reconcile: skipped: {reconciled['skipped']}")
        for line in reconciled.get("outside", []):
            typer.echo(f"reconcile: outside tolerance: {line}")
    typer.echo(
        f"nightly {day}: {len(detail['games'])} final games, {len(detail['fetched'])} fetched, "
        f"{len(detail['parsed'])} parsed"
    )


@app.command("weekly", help=COMMANDS["weekly"])
def weekly() -> None:
    settings = Settings()
    with _open_jobs(settings, SystemClock()) as jobs:
        try:
            detail = jobs.weekly()
        except Exception as exc:
            typer.echo(f"weekly: failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(1) from None
    shots = detail["ran"]["shots"]
    for failure in shots["failed"]:
        typer.echo(f"shotchartdetail {failure['key']}: failed: {failure['error']}")
    for key, error in shots["parse_failed"].items():
        typer.echo(f"parse shots {key}: failed: {error}")
    typer.echo(
        f"weekly {shots['season']}: {shots['shot_charts']} shot charts, "
        f"{shots['fetched']} fetched, {shots['rows']} shots loaded"
    )
    for name in detail["ran"]:
        if name != "shots":
            typer.echo(f"weekly: overlay {name}: succeeded")


@app.command("overlays", help=COMMANDS["overlays"])
def overlays(
    name: Annotated[str, typer.Argument(help="The overlay to run.")],
    date: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="The date to run it for. Defaults to today in TZ."),
    ] = None,
) -> None:
    try:
        by_name(name)
    except UnknownOverlayError as exc:
        raise typer.BadParameter(str(exc), param_hint="NAME") from None
    settings = Settings()
    clock = SystemClock()
    day = date.date() if date else clock.now().astimezone(settings.tz).date()
    with _open_jobs(settings, clock) as jobs:
        try:
            jobs.overlay(name, day)
        except Exception as exc:
            typer.echo(f"overlays {name} {day}: failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(1) from None
    typer.echo(f"overlays {name} {day}: succeeded")


@app.command("worker", help=COMMANDS["worker"])
def worker() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    # Before the signal handlers, so a SIGTERM during the wait stops the process outright (the
    # compose services run with `init: true`, so this is not PID 1, which ignores it).
    engine = make_engine(settings)
    try:
        wait_for_schema_head(
            engine,
            timeout=timedelta(seconds=settings.schema_wait_timeout_seconds),
            clock=SystemClock(),
        )
    except SchemaNotAtHeadError as exc:
        log.error("worker not started: %s", exc)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    # Docker stops a container with SIGTERM.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, on_signal)
    with _open_jobs(settings, SystemClock()) as jobs:
        scheduler = build_scheduler(jobs)
        scheduler.start()  # pyright: ignore[reportUnknownMemberType]
        log.info("worker started")
        # Polled rather than `stop.wait()`, which holds the lock `set()` needs (see `SystemClock`).
        while not stop.is_set():
            time.sleep(POLL_SECONDS)
        # Waits for a job under way to finish.
        scheduler.shutdown()  # pyright: ignore[reportUnknownMemberType]
    log.info("worker stopped")


@app.command("api", help=COMMANDS["api"])
def api(
    # All interfaces, since it runs in a container; kumadrome publishes it on 127.0.0.1 only.
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Restart on source changes (local dev).")] = False,
) -> None:
    uvicorn.run(
        "closinglineup.api.app:asgi",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(Path(__file__).parent)] if reload else None,
        # uvicorn's loggers propagate to the root JSON handler `asgi` installs.
        log_config=None,
    )

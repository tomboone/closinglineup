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
from typing import Annotated

import httpx
import typer
import uvicorn
from sqlalchemy.exc import OperationalError

from closinglineup.backfill import fetch_season
from closinglineup.clock import POLL_SECONDS, Clock, SystemClock
from closinglineup.db.models import RunStatus
from closinglineup.db.session import make_engine, make_session_factory, ping
from closinglineup.heartbeat import Heartbeat
from closinglineup.load.loader import LoadResult, load_dates, load_keyed
from closinglineup.load.memberships import load_participation_spans
from closinglineup.load.parquet import ParquetStore
from closinglineup.logging import configure_logging
from closinglineup.pipeline import (
    SHOT_TABLE,
    NotCachedError,
    game_date_of,
    parse_date,
    parse_shot_charts,
    shot_chart_keys,
)
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
    fetch_rosters,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)

log = logging.getLogger(__name__)

COMMANDS = {
    "fetch": "Fetch raw responses from NBA.com into the raw cache.",
    "parse": "Parse cached raw responses into parquet rows.",
    "load": "Load parquet rows into Postgres.",
    "derive": "Refresh windows and write rotation state, events and flags.",
    "overlays": "Fetch injuries, transactions, impact metrics and contracts.",
    "api": "Serve the API and the built frontend.",
    "worker": "Run the job scheduler.",
    "nightly": "Run the nightly job once: refresh the schedule, fetch, parse and load a date.",
    "weekly": "Run the weekly job once: re-fetch, parse and load the season's shot charts.",
    "health": "Check that the database answers.",
    "reparse": "Re-parse cached raw responses after a parser change.",
}


@app.command("health", help=COMMANDS["health"])
def health() -> None:
    engine = make_engine(Settings())
    try:
        ping(engine)
    except OperationalError as exc:
        typer.echo(f"health: database unreachable: {exc.orig}", err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
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
    force: Annotated[bool, typer.Option(help="Re-fetch responses already cached.")] = False,
) -> None:
    modes = sum([game_id is not None, rosters, shots, aggregates])
    walk = modes == 0 and season is not None
    if modes != 1 and not walk:
        raise typer.BadParameter(
            "pass exactly one of --game-id, --rosters, --shots, --aggregates, or --season alone"
        )
    if shots and player_id is None:
        raise typer.BadParameter("--shots needs --player-id")
    if (start is not None or end is not None) and not walk:
        raise typer.BadParameter("--from and --to go with --season alone")
    if start is not None and end is not None and start > end:
        raise typer.BadParameter("--from is after --to")
    settings = Settings()
    clock = SystemClock()
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
    if any(result.status is FetchStatus.FAILED for result in results):
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
        for failed_game, error in report.failed.items():
            typer.echo(f"parse {day} {failed_game}: failed: {error}")
        for missing_game in report.missing:
            typer.echo(f"parse {day} {missing_game}: final, but no box score cached")
        typer.echo(f"parse {day}: {len(report.parsed)} games parsed")
        failed = failed or bool(report.failed)
    if failed:
        raise typer.Exit(1)


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
            help="Rebuild --season's roster memberships from its loaded games.",
        ),
    ] = False,
    season: Annotated[
        str | None,
        typer.Option(help="With --memberships: e.g. 2026-27. Defaults to the current one."),
    ] = None,
) -> None:
    days = _days(date, start, end)
    if shots and memberships:
        raise typer.BadParameter("pass --shots or --memberships, not both")
    if (shots or memberships) and (days is not None or missing):
        raise typer.BadParameter("--shots and --memberships take no dates and no --missing")
    if days is None and not missing and not shots and not memberships:
        raise typer.BadParameter("pass --date, --from/--to, --missing, --shots or --memberships")
    if season is not None and not memberships:
        raise typer.BadParameter("--season goes with --memberships")
    settings = Settings()
    engine = make_engine(settings)
    db = make_session_factory(engine)
    store = ParquetStore(settings.data_dir)
    try:
        if memberships:
            season = season or season_label(SystemClock().now().astimezone(settings.tz).date())
            results: list[LoadResult] = [load_participation_spans(db, season)]
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
    for failed_game, error in detail["parse_failed"].items():
        typer.echo(f"parse {failed_game}: failed: {error}")
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
    for failure in detail["failed"]:
        typer.echo(f"shotchartdetail {failure['key']}: failed: {failure['error']}")
    for key, error in detail["parse_failed"].items():
        typer.echo(f"parse shots {key}: failed: {error}")
    typer.echo(
        f"weekly {detail['season']}: {detail['shot_charts']} shot charts, "
        f"{detail['fetched']} fetched, {detail['rows']} shots loaded"
    )


@app.command("worker", help=COMMANDS["worker"])
def worker() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
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


def _stub(name: str, help_text: str) -> None:
    def command() -> None:
        typer.echo(f"{name}: not implemented", err=True)
        raise typer.Exit(1)

    app.command(name, help=help_text)(command)


# Each later ticket replaces a stub by registering the real command above.
_implemented = {command.name for command in app.registered_commands}
for _name, _help in COMMANDS.items():
    if _name not in _implemented:
        _stub(_name, _help)

"""Command-line entry point: every job is a command (spec §3.1)."""

import logging
import signal
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Annotated

import httpx
import typer
import uvicorn
from sqlalchemy.exc import OperationalError

from closinglineup.clock import POLL_SECONDS, Clock, SystemClock
from closinglineup.db.session import make_engine, make_session_factory, ping
from closinglineup.heartbeat import Heartbeat
from closinglineup.logging import configure_logging
from closinglineup.scheduler import Jobs, build_scheduler
from closinglineup.season import season_label
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import make_nba_session
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
    "nightly": "Run the nightly job once: refresh the schedule, fetch a date's final games.",
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
        str | None, typer.Option(help="Season, e.g. 2026-27. Defaults to the current one.")
    ] = None,
    season_type: Annotated[SeasonType, typer.Option(help="Season type.")] = (
        SeasonType.REGULAR_SEASON
    ),
    force: Annotated[bool, typer.Option(help="Re-fetch responses already cached.")] = False,
) -> None:
    modes = sum([game_id is not None, rosters, shots, aggregates])
    if modes != 1:
        raise typer.BadParameter("pass exactly one of --game-id, --rosters, --shots, --aggregates")
    if shots and player_id is None:
        raise typer.BadParameter("--shots needs --player-id")
    settings = Settings()
    clock = SystemClock()
    cache = RawCache(settings.data_dir, clock)
    season = season or season_label(clock.now().astimezone(settings.tz).date())
    with make_nba_session(settings, clock) as session:
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
    for result in results:
        reason = f": {result.error}" if result.error else ""
        typer.echo(f"{result.endpoint} {result.key}: {result.status}{reason}")
    if any(result.status is FetchStatus.FAILED for result in results):
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
    typer.echo(
        f"nightly {day}: {len(detail['games'])} final games, {len(detail['fetched'])} fetched"
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

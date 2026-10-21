"""Command-line entry point: every job is a command (spec §3.1)."""

from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy.exc import OperationalError

from closinglineup.db.session import make_engine, ping
from closinglineup.settings import Settings

app = typer.Typer(no_args_is_help=True, add_completion=False)

COMMANDS = {
    "fetch": "Fetch raw responses from NBA.com into the raw cache.",
    "parse": "Parse cached raw responses into parquet rows.",
    "load": "Load parquet rows into Postgres.",
    "derive": "Refresh windows and write rotation state, events and flags.",
    "overlays": "Fetch injuries, transactions, impact metrics and contracts.",
    "api": "Serve the API and the built frontend.",
    "worker": "Run the job scheduler.",
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

"""Command-line entry point: every job is a command (spec §3.1)."""

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)

# Each later ticket replaces its stub with the real command.
COMMANDS = {
    "fetch": "Fetch raw responses from NBA.com into the raw cache.",
    "parse": "Parse cached raw responses into parquet rows.",
    "load": "Load parquet rows into Postgres.",
    "derive": "Refresh windows and write rotation state, events and flags.",
    "overlays": "Fetch injuries, transactions, impact metrics and contracts.",
    "api": "Serve the API and the built frontend.",
    "worker": "Run the job scheduler.",
    "health": "Report whether the last jobs ran.",
    "reparse": "Re-parse cached raw responses after a parser change.",
}


def _stub(name: str, help_text: str) -> None:
    def command() -> None:
        typer.echo(f"{name}: not implemented", err=True)
        raise typer.Exit(1)

    app.command(name, help=help_text)(command)


for _name, _help in COMMANDS.items():
    _stub(_name, _help)

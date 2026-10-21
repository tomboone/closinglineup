import re

import pytest
from typer.testing import CliRunner

from closinglineup.cli import COMMANDS, app

runner = CliRunner()

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
EXPECTED = ["fetch", "parse", "load", "derive", "overlays", "api", "worker", "health", "reparse"]
STUBS = [command for command in EXPECTED if command != "health"]


def test_help_lists_the_nine_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = ANSI_ESCAPE.sub("", result.output)
    assert list(COMMANDS) == EXPECTED
    for command in EXPECTED:
        assert re.search(rf"^\W*{command}\s", output, re.MULTILINE), command


@pytest.mark.parametrize("command", STUBS)
def test_stub_reports_not_implemented_and_exits_1(command: str) -> None:
    result = runner.invoke(app, [command])

    assert result.exit_code == 1
    assert f"{command}: not implemented" in result.output


def test_health_exits_0_when_the_database_answers(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0, result.output
    assert "health: ok" in result.output


def test_health_exits_1_when_the_database_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # Port 1 on loopback refuses at once, standing in for a stopped Postgres.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://closinglineup:secret@127.0.0.1:1/x")

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "health: database unreachable" in result.output
    assert "secret" not in result.output

import re

import pytest
from typer.testing import CliRunner

from closinglineup.cli import COMMANDS, app

runner = CliRunner()

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
EXPECTED = ["fetch", "parse", "load", "derive", "overlays", "api", "worker", "health", "reparse"]


def test_help_lists_the_nine_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = ANSI_ESCAPE.sub("", result.output)
    assert list(COMMANDS) == EXPECTED
    for command in EXPECTED:
        assert re.search(rf"^\W*{command}\s", output, re.MULTILINE), command


@pytest.mark.parametrize("command", EXPECTED)
def test_stub_reports_not_implemented_and_exits_1(command: str) -> None:
    result = runner.invoke(app, [command])

    assert result.exit_code == 1
    assert f"{command}: not implemented" in result.output

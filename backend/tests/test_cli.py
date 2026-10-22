import re
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from curl_cffi import requests
from typer.testing import CliRunner

from closinglineup import cli
from closinglineup.cli import COMMANDS, app
from closinglineup.clock import Clock
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaSession, RateLimiter
from tests.fakes import ScriptedGet, fake_response
from tests.test_nba_stats import ENDPOINTS, GAME_ID, fixture_body

runner = CliRunner()

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
EXPECTED = ["fetch", "parse", "load", "derive", "overlays", "api", "worker", "health", "reparse"]
STUBS = [command for command in EXPECTED if command not in {"fetch", "health", "api"}]


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


def test_api_serves_the_app_factory_with_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_run(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = runner.invoke(app, ["api", "--port", "8123"])

    assert result.exit_code == 0, result.output
    [(args, kwargs)] = calls
    assert args == ("closinglineup.api.app:asgi",)
    assert kwargs["factory"] is True
    assert (kwargs["host"], kwargs["port"], kwargs["reload"]) == ("0.0.0.0", 8123, False)
    assert kwargs["reload_dirs"] is None


@pytest.fixture
def scripted_nba(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ScriptedGet:
    """Points `fetch` at a raw cache under `tmp_path` and an NbaSession that never hits the
    network; the test scripts the returned `get`'s responses by appending to its outcomes."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get = ScriptedGet()

    def fake_session(settings: Settings, clock: Clock) -> NbaSession:
        session = NbaSession(requests.Session(), RateLimiter(1e6, clock), clock)
        monkeypatch.setattr(session.curl, "get", get)
        return session

    monkeypatch.setattr(cli, "make_nba_session", fake_session)
    return get


def _cache_game(data_dir: Path) -> None:
    """Every endpoint cached, as in `test_nba_stats._cache_all`; `playbyplayv2` only by hand."""
    cache = RawCache(data_dir)
    for endpoint in ENDPOINTS:
        url = f"https://stats.nba.com/stats/{endpoint}"
        cache.put("nba_stats", endpoint, GAME_ID, fixture_body(endpoint), url=url, status=200)


def test_fetch_game_id_on_a_cached_game_makes_no_requests(
    scripted_nba: ScriptedGet, tmp_path: Path
) -> None:
    _cache_game(tmp_path)

    result = runner.invoke(app, ["fetch", "--game-id", GAME_ID])

    assert result.exit_code == 0, result.output
    assert scripted_nba.calls == []
    assert result.output.splitlines() == [f"{endpoint}: cached" for endpoint in ENDPOINTS]


def test_fetch_game_id_with_force_makes_eight_requests_and_fails_on_a_failed_endpoint(
    scripted_nba: ScriptedGet, tmp_path: Path
) -> None:
    _cache_game(tmp_path)
    scripted_nba.outcomes.extend(fake_response(200, fixture_body(e)) for e in ENDPOINTS)

    result = runner.invoke(app, ["fetch", "--game-id", GAME_ID, "--force"])

    assert result.exit_code == 1
    assert len(scripted_nba.calls) == 8
    assert result.output.splitlines()[-1] == (
        f"playbyplayv2: failed: playbyplayv2 has no data for game {GAME_ID}"
    )

import copy
import json
import re
import signal
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import FrameType
from typing import Any, cast

import pytest
import uvicorn
from apscheduler.schedulers.background import (  # pyright: ignore[reportMissingTypeStubs]
    BackgroundScheduler,
)
from curl_cffi import requests
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from closinglineup import cli
from closinglineup.cli import COMMANDS, app
from closinglineup.clock import Clock, FakeClock
from closinglineup.db.models import JobRun, LoadLog, PlayerGame
from closinglineup.load.parquet import ROW_MODELS, ParquetStore
from closinglineup.overlays import Overlay, register_overlay
from closinglineup.scheduler import DAILY_OVERLAYS, INJURIES, NIGHTLY, POLL_FINALS, WEEKLY
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaSession, RateLimiter
from closinglineup.teams import TEAMS
from tests.fakes import Connect, FakeOverlay, RoutedGet, ScriptedGet, fake_response
from tests.test_backfill import InterruptedGet, season_routes
from tests.test_derive_roster import UNCOVERED
from tests.test_loader import clear_fixture_game
from tests.test_nba_stats import ENDPOINTS, GAME_ID, fixture_body, season_fixture_body
from tests.test_pipeline import DAY, GAME, UNCACHED, cache_fixture_game
from tests.test_pipeline import SCHEDULE as FIXTURE_SCHEDULE
from tests.test_scheduler import FINAL_A, FINAL_B, SCHEDULE, cdn_game_url, nba_routes

runner = CliRunner()

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
EXPECTED = [
    "fetch",
    "parse",
    "load",
    "derive",
    "overlays",
    "api",
    "worker",
    "nightly",
    "weekly",
    "health",
    "reparse",
    "reconcile",
]


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = ANSI_ESCAPE.sub("", result.output)
    assert list(COMMANDS) == EXPECTED
    for command in EXPECTED:
        assert re.search(rf"^\W*{command}\s", output, re.MULTILINE), command


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
        return NbaSession(Connect(get), RateLimiter(1e6, clock), clock)

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
    assert result.output.splitlines() == [f"{e} {GAME_ID}: cached" for e in ENDPOINTS]


def test_fetch_game_id_with_force_makes_eight_requests_and_fails_on_a_failed_endpoint(
    scripted_nba: ScriptedGet, tmp_path: Path
) -> None:
    _cache_game(tmp_path)
    scripted_nba.outcomes.extend(fake_response(200, fixture_body(e)) for e in ENDPOINTS)

    result = runner.invoke(app, ["fetch", "--game-id", GAME_ID, "--force"])

    assert result.exit_code == 1
    assert len(scripted_nba.calls) == 8
    assert result.output.splitlines()[-1] == (
        f"playbyplayv2 {GAME_ID}: failed: playbyplayv2 has no data for game {GAME_ID}"
    )


def test_fetch_rosters_writes_a_roster_per_team_and_the_player_list(
    scripted_nba: ScriptedGet, tmp_path: Path
) -> None:
    roster = fake_response(200, season_fixture_body("commonteamroster"))
    players = fake_response(200, season_fixture_body("commonallplayers"))
    scripted_nba.outcomes.extend([*[roster] * 30, players])

    result = runner.invoke(app, ["fetch", "--rosters", "--season", "2026-27"])

    assert result.exit_code == 0, result.output
    rosters = RawCache(tmp_path).list("nba_stats", "commonteamroster")
    assert rosters == sorted(f"{t.nba_team_id}_2026-27" for t in TEAMS)
    assert RawCache(tmp_path).list("nba_stats", "commonallplayers") == ["2026-27"]
    assert result.output.splitlines()[0] == "commonteamroster 1610612737_2026-27: fetched"


def test_fetch_shots_fetches_the_player_for_the_season_type(
    scripted_nba: ScriptedGet, tmp_path: Path
) -> None:
    scripted_nba.outcomes.append(fake_response(200, season_fixture_body("shotchartdetail")))

    args = ["--shots", "--player-id", "201939", "--season", "2025-26", "--season-type", "Playoffs"]
    result = runner.invoke(app, ["fetch", *args])

    assert result.exit_code == 0, result.output
    assert result.output == "shotchartdetail 201939_2025-26_playoffs: fetched\n"
    [(_, params)] = scripted_nba.calls
    assert params is not None
    assert (params["PlayerID"], params["SeasonType"]) == ("201939", "Playoffs")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "pass exactly one of"),
        (["--rosters", "--aggregates"], "pass exactly one of"),
        (["--missing", "--rosters"], "pass exactly one of"),
        (["--missing", "--force"], "--missing takes no --season and no --force"),
        (["--game-id", GAME_ID, "--shots"], "pass exactly one of"),
        (["--shots"], "--shots needs --player-id"),
        (["--rosters", "--from", "2025-10-23"], "--from and --to go with --season alone"),
        (["--season", "2025-26", "--from", "2025-10-24", "--to", "2025-10-23"], "after --to"),
    ],
)
def test_fetch_without_exactly_one_mode_is_a_usage_error(
    scripted_nba: ScriptedGet, args: list[str], message: str
) -> None:
    result = runner.invoke(app, ["fetch", *args])

    assert result.exit_code == 2
    assert message in ANSI_ESCAPE.sub("", result.output)
    assert scripted_nba.calls == []


def test_fetch_season_reports_each_game_and_a_rerun_skips_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _route_nba(monkeypatch, RoutedGet(season_routes()))
    args = ["fetch", "--season", "2026-27", "--from", "2026-10-20"]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert first.output.splitlines() == [
        f"2026-10-20 {FINAL_A}: fetched",
        f"2026-10-20 {FINAL_B}: fetched",
        "fetch 2026-27: 2 final games, 0 already cached, 2 fetched, 0 with failures",
    ]
    assert second.output == (
        "fetch 2026-27: 2 final games, 2 already cached, 0 fetched, 0 with failures\n"
    )


def test_fetch_season_exits_1_on_a_failed_endpoint_or_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    routes = season_routes()
    del routes[cdn_game_url(nba_cdn.BOXSCORE, FINAL_B)]
    _route_nba(monkeypatch, RoutedGet(routes))

    failed = runner.invoke(app, ["fetch", "--season", "2026-27", "--from", "2026-10-20"])
    no_schedule = runner.invoke(app, ["fetch", "--season", "2025-26"])

    assert failed.exit_code == 1
    assert failed.output.splitlines()[1:] == [
        f"boxscore {FINAL_B}: failed: HTTP 404 from {cdn_game_url(nba_cdn.BOXSCORE, FINAL_B)}",
        f"2026-10-20 {FINAL_B}: 1 failed",
        "fetch 2026-27: 2 final games, 0 already cached, 2 fetched, 1 with failures",
    ]
    assert no_schedule.exit_code == 1
    # The routed schedule is 2026-27's, which is no 2025-26 schedule.
    assert no_schedule.output == (
        "fetch 2025-26: failed: scheduleleaguev2 has no data for 2025-26\n"
    )


def test_fetch_season_waits_out_nba_com_then_stops_cleanly_if_it_still_does_not_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    timeout = requests.exceptions.Timeout("curl: (28) Operation timed out")
    _route_nba(monkeypatch, InterruptedGet(season_routes(), at=2, error=timeout))
    # A fake clock, so the backoff and the pauses take no time.
    clock = FakeClock(datetime(2026, 10, 22, 12, tzinfo=UTC))
    monkeypatch.setattr(cli, "SystemClock", lambda: clock)

    result = runner.invoke(app, ["fetch", "--season", "2026-27", "--from", "2026-10-20"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    *paused, stopped, resume = result.output.splitlines()
    assert paused == [
        f"2026-10-20 {FINAL_A}: no answer from NBA.com, trying again in {minutes} min"
        for minutes in (5, 15, 30)
    ]
    assert stopped.startswith(f"fetch 2026-27: stopped: game {FINAL_A} of 2026-10-20: no answer")
    assert resume == "fetch 2026-27: run the same command again to resume"


def _route_nba(monkeypatch: pytest.MonkeyPatch, get: RoutedGet) -> None:
    def fake_session(settings: Settings, clock: Clock) -> NbaSession:
        return NbaSession(Connect(get), RateLimiter(1e6, clock), clock)

    monkeypatch.setattr(cli, "make_nba_session", fake_session)


@pytest.fixture
def nightly_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HEALTHCHECKS_URL_NIGHTLY", raising=False)


@pytest.mark.usefixtures("nightly_env")
def test_nightly_date_fetches_that_dates_final_games_and_records_the_run(
    monkeypatch: pytest.MonkeyPatch, db: sessionmaker[Session]
) -> None:
    get = RoutedGet(nba_routes(SCHEDULE, FINAL_A, FINAL_B))
    _route_nba(monkeypatch, get)

    result = runner.invoke(app, ["nightly", "--date", "2026-10-20"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == (
        "nightly 2026-10-20: 2 final games, 2 fetched, 0 parsed"
    )
    with db() as session:
        [run] = session.scalars(select(JobRun))
    assert (run.job, run.status) == (NIGHTLY, "succeeded")
    assert run.detail is not None
    assert run.detail["fetched"] == [FINAL_A, FINAL_B]


@pytest.mark.usefixtures("nightly_env")
def test_overlays_runs_one_overlay_for_the_date_and_records_the_run(
    db: sessionmaker[Session], registry: dict[str, Overlay]
) -> None:
    epm, darko = FakeOverlay(), FakeOverlay(RuntimeError("app down"))
    register_overlay("epm", "daily", epm)
    register_overlay("darko", "daily", darko)

    ran = runner.invoke(app, ["overlays", "epm", "--date", "2026-10-20"])
    failed = runner.invoke(app, ["overlays", "darko", "--date", "2026-10-20"])

    assert ran.exit_code == 0, ran.output
    assert ran.output.splitlines()[-1] == "overlays epm 2026-10-20: succeeded"
    assert [day for _, day in epm.calls] == [date(2026, 10, 20)]
    assert failed.exit_code == 1
    assert "overlays darko 2026-10-20: failed: RuntimeError: app down" in failed.output
    with db() as session:
        runs = session.scalars(select(JobRun).order_by(JobRun.id))
        assert [(run.job, run.status) for run in runs] == [
            ("overlay:epm", "succeeded"),
            ("overlay:darko", "failed"),
        ]


def test_overlays_rejects_an_unregistered_name(registry: dict[str, Overlay]) -> None:
    register_overlay("epm", "daily", FakeOverlay())

    result = runner.invoke(app, ["overlays", "darko"])

    assert result.exit_code == 2
    assert "no overlay named 'darko'; registered: epm" in ANSI_ESCAPE.sub("", result.output)


@pytest.mark.usefixtures("nightly_env")
def test_nightly_exits_1_when_the_job_fails(
    monkeypatch: pytest.MonkeyPatch, db: sessionmaker[Session]
) -> None:
    _route_nba(monkeypatch, RoutedGet(nba_routes(None)))

    result = runner.invoke(app, ["nightly", "--date", "2026-10-20"])

    assert result.exit_code == 1
    assert "nightly 2026-10-20: failed: NbaHttpError: HTTP 404" in result.output
    with db() as session:
        assert [run.status for run in session.scalars(select(JobRun))] == ["failed"]


def test_worker_runs_the_scheduler_with_every_job_until_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/x")
    handlers: dict[int, Callable[[int, FrameType | None], None]] = {}
    calls: list[object] = []

    def fake_signal(signum: int, handler: Callable[[int, FrameType | None], None]) -> None:
        handlers[signum] = handler

    def fake_start(scheduler: BackgroundScheduler) -> None:
        jobs = cast(list[Any], scheduler.get_jobs())  # pyright: ignore[reportUnknownMemberType]
        calls.append(sorted(job.id for job in jobs))
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    def fake_shutdown(scheduler: BackgroundScheduler) -> None:
        calls.append("shutdown")

    def no_logging(level: str) -> None:
        pass

    # Neither the root logger nor the test process's signal handlers are the test's to change.
    monkeypatch.setattr(cli, "configure_logging", no_logging)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(BackgroundScheduler, "start", fake_start)
    monkeypatch.setattr(BackgroundScheduler, "shutdown", fake_shutdown)

    result = runner.invoke(app, ["worker"])

    assert result.exit_code == 0, result.output
    assert calls == [sorted([NIGHTLY, POLL_FINALS, WEEKLY, INJURIES, DAILY_OVERLAYS]), "shutdown"]
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}


@pytest.fixture
def data_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """`parse` and `load` pointed at a data directory under `tmp_path`, with no database."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


def test_parse_date_writes_every_tables_file_for_the_date(data_env: Path) -> None:
    cache_fixture_game(data_env)

    result = runner.invoke(app, ["parse", "--date", "2025-10-23"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"parse 2025-10-23 {UNCACHED}: final, but no box score cached",
        "parse 2025-10-23: 1 games parsed",
    ]
    store = ParquetStore(data_env)
    assert all(store.path(table, DAY).is_file() for table in ROW_MODELS)


def test_parse_from_to_parses_each_date_in_the_range(data_env: Path) -> None:
    cache_fixture_game(data_env)

    result = runner.invoke(app, ["parse", "--from", "2025-10-22", "--to", "2025-10-24"])

    assert result.exit_code == 0, result.output
    assert ParquetStore(data_env).dates() == [
        date(2025, 10, 22),
        date(2025, 10, 23),
        date(2025, 10, 24),
    ]


def test_parse_game_id_parses_that_games_date(data_env: Path) -> None:
    cache_fixture_game(data_env)

    result = runner.invoke(app, ["parse", "--game-id", GAME])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "parse 2025-10-23: 1 games parsed"


def test_parse_exits_1_when_the_schedule_is_not_cached(data_env: Path) -> None:
    result = runner.invoke(app, ["parse", "--date", "2025-10-23"])

    assert result.exit_code == 1
    assert result.output == "parse 2025-10-23: failed: no 2025-26 schedule in the raw cache\n"


@pytest.mark.parametrize(
    ("command", "args", "message"),
    [
        ("parse", [], "pass exactly one of"),
        ("parse", ["--date", "2025-10-23", "--game-id", GAME], "pass exactly one of"),
        ("parse", ["--date", "2025-10-23", "--from", "2025-10-22"], "not both"),
        ("parse", ["--from", "2025-10-22"], "--from and --to go together"),
        ("parse", ["--from", "2025-10-24", "--to", "2025-10-23"], "--from is after --to"),
        ("parse", ["--date", "2025-10-23", "--shots"], "pass exactly one of"),
        ("parse", ["--date", "2025-10-23", "--season", "2025-26"], "--season goes with --shots"),
        ("load", [], "pass --date, --from/--to, --missing, --shots or --memberships"),
        ("load", ["--shots", "--missing"], "take no dates and no --missing"),
        ("load", ["--shots", "--date", "2025-10-23"], "take no dates and no --missing"),
        ("load", ["--memberships", "--missing"], "take no dates and no --missing"),
        ("load", ["--shots", "--memberships"], "pass --shots or --memberships, not both"),
        ("derive", ["--from", "2025-10-22", "--missing"], "--from and --to go together"),
    ],
)
def test_parse_load_and_derive_without_dates_are_usage_errors(
    data_env: Path, command: str, args: list[str], message: str
) -> None:
    result = runner.invoke(app, [command, *args])

    assert result.exit_code == 2
    assert message in ANSI_ESCAPE.sub("", result.output)


SHOT_CHART = "201939_2025-26_regular-season"


def _cache_shot_chart(data_dir: Path, key: str, body: bytes) -> None:
    RawCache(data_dir).put(
        nba_stats.SOURCE, nba_stats.SHOT_CHART_DETAIL, key, body, url="u", status=200
    )


def test_parse_shots_parses_every_cached_shot_chart_of_the_season(data_env: Path) -> None:
    _cache_shot_chart(data_env, SHOT_CHART, season_fixture_body(nba_stats.SHOT_CHART_DETAIL))
    _cache_shot_chart(data_env, "201939_2026-27_regular-season", b"{}")

    result = runner.invoke(app, ["parse", "--shots", "--season", "2025-26"])

    assert result.exit_code == 0, result.output
    assert result.output == "parse shots 2025-26: 1 shot charts parsed\n"
    assert ParquetStore(data_env).keys("shot") == [SHOT_CHART]


def test_parse_shots_exits_1_when_a_shot_chart_fails(data_env: Path) -> None:
    _cache_shot_chart(data_env, SHOT_CHART, b"{}")

    result = runner.invoke(app, ["parse", "--shots", "--season", "2025-26"])

    assert result.exit_code == 1
    assert result.output.splitlines() == [
        f"parse shots {SHOT_CHART}: failed: ParseError: payload has no Shot_Chart_Detail "
        "result set",
        "parse shots 2025-26: 0 shot charts parsed",
    ]


@pytest.fixture
def load_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str, db: sessionmaker[Session]
) -> Path:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    clear_fixture_game(db)
    return tmp_path


def test_load_missing_reloads_only_the_date_whose_load_log_row_is_gone(
    load_env: Path, db: sessionmaker[Session]
) -> None:
    cache_fixture_game(load_env)
    runner.invoke(app, ["parse", "--from", "2025-10-22", "--to", "2025-10-23"])
    first = runner.invoke(app, ["load", "--from", "2025-10-22", "--to", "2025-10-23"])
    assert first.exit_code == 0, first.output
    assert len(first.output.splitlines()) == 2 * len(ROW_MODELS)
    with db.begin() as session:
        session.execute(
            delete(LoadLog).where(LoadLog.table_name == "game", LoadLog.game_date == DAY)
        )

    result = runner.invoke(app, ["load", "--missing"])

    assert result.exit_code == 0, result.output
    assert result.output == "load game 2025-10-23: succeeded, 2 rows, 0 changed, 0 removed\n"
    assert runner.invoke(app, ["load", "--missing"]).output == "load: nothing to load\n"


def test_load_exits_1_when_a_table_fails(load_env: Path) -> None:
    result = runner.invoke(app, ["load", "--date", "2025-10-23"])

    assert result.exit_code == 1
    assert result.output.splitlines()[0].startswith(
        "load game 2025-10-23: failed: FileNotFoundError"
    )


def test_nightly_date_fetches_parses_and_loads_the_fixture_game(
    monkeypatch: pytest.MonkeyPatch, load_env: Path, db: sessionmaker[Session]
) -> None:
    monkeypatch.delenv("HEALTHCHECKS_URL_NIGHTLY", raising=False)
    schedule = copy.deepcopy(FIXTURE_SCHEDULE)
    [game_date] = schedule["leagueSchedule"]["gameDates"]
    game_date["games"] = [g for g in game_date["games"] if g["gameId"] == GAME]
    pbp = Path(__file__).parent / "fixtures" / "pbp"
    routes = {
        nba_cdn.SCHEDULE_URL: json.dumps(schedule).encode(),
        **{
            f"{nba_stats.STATS_BASE}/{endpoint}": fixture_body(endpoint)
            for endpoint in nba_stats.GAME_ENDPOINTS
        },
        **{
            cdn_game_url(endpoint, GAME): (pbp / endpoint / f"{GAME}.json").read_bytes()
            for endpoint in (nba_cdn.BOXSCORE, nba_cdn.PLAYBYPLAY)
        },
    }
    _route_nba(monkeypatch, RoutedGet(routes))

    result = runner.invoke(app, ["nightly", "--date", "2025-10-23"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == (
        "nightly 2025-10-23: 1 final games, 1 fetched, 1 parsed"
    )
    with db() as session:
        [run] = session.scalars(select(JobRun))
        count = select(func.count()).select_from(PlayerGame).where(PlayerGame.game_id == GAME)
        assert session.scalar(count) == 28
    assert (run.job, run.status) == (NIGHTLY, "succeeded")
    assert run.detail is not None
    assert run.detail["loaded"]["player_game"] == 28
    assert run.detail["memberships"] >= 28


def test_fetch_missing_replaces_the_cdn_rows_of_a_game_nightly_could_not_fetch_from_stats(
    monkeypatch: pytest.MonkeyPatch, load_env: Path, db: sessionmaker[Session]
) -> None:
    monkeypatch.delenv("HEALTHCHECKS_URL_NIGHTLY", raising=False)
    schedule = copy.deepcopy(FIXTURE_SCHEDULE)
    [game_date] = schedule["leagueSchedule"]["gameDates"]
    game_date["games"] = [g for g in game_date["games"] if g["gameId"] == GAME]
    pbp = Path(__file__).parent / "fixtures" / "pbp"
    # Only cdn.nba.com answers.
    get = RoutedGet(
        {
            nba_cdn.SCHEDULE_URL: json.dumps(schedule).encode(),
            **{
                cdn_game_url(endpoint, GAME): (pbp / endpoint / f"{GAME}.json").read_bytes()
                for endpoint in (nba_cdn.BOXSCORE, nba_cdn.PLAYBYPLAY)
            },
        }
    )
    _route_nba(monkeypatch, get)

    def sources() -> list[str]:
        with db() as session:
            query = select(PlayerGame.source).where(PlayerGame.game_id == GAME)
            return list(session.scalars(query))

    nightly = runner.invoke(app, ["nightly", "--date", "2025-10-23"])

    assert nightly.exit_code == 0, nightly.output
    assert f"warning: {GAME}: stats.nba.com failed for boxscoretraditionalv3" in nightly.output
    assert sources() == ["cdn"] * 28

    get.routes.update(
        {
            f"{nba_stats.STATS_BASE}/{endpoint}": fixture_body(endpoint)
            for endpoint in nba_stats.GAME_ENDPOINTS
        }
    )
    healed = runner.invoke(app, ["fetch", "--missing"])

    assert healed.exit_code == 0, healed.output
    assert healed.output.splitlines()[-1] == (
        "fetch --missing: 1 stats_failed games, 1 fetched, 0 still failing"
    )
    assert sources() == ["stats"] * 28


def test_load_memberships_rebuilds_the_spans_from_the_loaded_games(
    load_env: Path, db: sessionmaker[Session]
) -> None:
    cache_fixture_game(load_env)
    runner.invoke(app, ["parse", "--date", "2025-10-23"])
    assert runner.invoke(app, ["load", "--date", "2025-10-23"]).exit_code == 0

    result = runner.invoke(app, ["load", "--memberships"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("load roster_membership all: succeeded, ")
    with db() as session:
        uncovered = session.execute(UNCOVERED, {"season": "2025-26"}).scalar_one()
    assert uncovered == 0


def test_load_shots_loads_every_parsed_shot_chart_and_a_rerun_changes_nothing(
    load_env: Path,
) -> None:
    cache_fixture_game(load_env)
    _cache_shot_chart(load_env, SHOT_CHART, season_fixture_body(nba_stats.SHOT_CHART_DETAIL))
    runner.invoke(app, ["parse", "--date", "2025-10-23"])
    assert runner.invoke(app, ["load", "--date", "2025-10-23"]).exit_code == 0
    assert runner.invoke(app, ["parse", "--shots", "--season", "2025-26"]).exit_code == 0

    first = runner.invoke(app, ["load", "--shots"])
    second = runner.invoke(app, ["load", "--shots"])

    assert first.exit_code == 0, first.output
    assert first.output == f"load shot {SHOT_CHART}: succeeded, 25 rows, 25 changed, 0 removed\n"
    assert second.output == f"load shot {SHOT_CHART}: succeeded, 25 rows, 0 changed, 0 removed\n"

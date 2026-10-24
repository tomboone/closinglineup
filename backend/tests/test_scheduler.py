import copy
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from curl_cffi import requests as curl_requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup import scheduler
from closinglineup.clock import FakeClock
from closinglineup.db.models import Game, JobRun, LoadLog, PlayerGame, RunStatus, Shot
from closinglineup.derive.reconcile import sample_teams
from closinglineup.derive.run import RECONCILE, WRITES
from closinglineup.heartbeat import Heartbeat
from closinglineup.load.loader import LOAD_STEP, LoadError, LoadResult
from closinglineup.load.parquet import ROW_MODELS, ParquetStore
from closinglineup.overlays import Overlay, UnknownOverlayError, register_overlay
from closinglineup.scheduler import (
    DAILY_OVERLAYS,
    INJURIES,
    NIGHTLY,
    POLL_FINALS,
    WEEKLY,
    JobFailedError,
    Jobs,
    build_scheduler,
    shot_chart_players,
)
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn, nba_http, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import (
    NbaHttpError,
    NbaSession,
    NbaUnreachableError,
    RateLimiter,
)
from closinglineup.sources.nba_stats import EndpointResult, SeasonType
from closinglineup.teams import by_nba_team_id
from tests.fakes import Connect, FakeOverlay, RoutedGet
from tests.test_loader import clear_fixture_game
from tests.test_nba_stats import GAME_ID, fixture_body, season_fixture_body
from tests.test_pipeline import DAY as FIXTURE_DAY
from tests.test_pipeline import GAME as FIXTURE_GAME
from tests.test_pipeline import PBP_FIXTURES, PLAYER_GAMES
from tests.test_pipeline import SCHEDULE as FIXTURE_SCHEDULE

CDN_FIXTURES = Path(__file__).parent / "fixtures" / "cdn"
NIGHTLY_URL = "https://hc-ping.com/nightly-123"
# 03:30 in New York, when the worker runs `nightly` for the day before.
START = datetime(2026, 10, 21, 7, 30, tzinfo=UTC)
DAY = date(2026, 10, 20)

FINAL_A = "0022600001"
FINAL_B = "0022600002"
SCHEDULED = "0022600003"
PRESEASON_FINAL = "0012600009"
FINAL_DAY_BEFORE = "0022600004"


def _game(game_id: str, status: int) -> dict[str, Any]:
    return {"gameId": game_id, "gameStatus": status, "gameSubtype": ""}


def _schedule_game(game_id: str, status: int, day: date) -> dict[str, Any]:
    """A schedule game, GSW hosting DEN, with what `parse.schedule` reads."""
    return {
        **_game(game_id, status),
        "gameDateEst": f"{day.isoformat()}T00:00:00Z",
        "homeTeam": {"teamId": 1610612744, "score": 100},
        "awayTeam": {"teamId": 1610612743, "score": 90},
    }


# Two final games on DAY, beside one scheduled, one preseason and one final the day before.
SCHEDULE = {
    "leagueSchedule": {
        "seasonYear": "2026-27",
        "gameDates": [
            {
                "gameDate": "10/19/2026 00:00:00",
                "games": [_schedule_game(FINAL_DAY_BEFORE, 3, DAY - timedelta(days=1))],
            },
            {
                "gameDate": "10/20/2026 00:00:00",
                "games": [
                    _schedule_game(PRESEASON_FINAL, 3, DAY),
                    _schedule_game(FINAL_A, 3, DAY),
                    _schedule_game(SCHEDULED, 1, DAY),
                    _schedule_game(FINAL_B, 3, DAY),
                ],
            },
        ],
    }
}


def _cdn_fixture(endpoint: str) -> bytes:
    (path,) = (CDN_FIXTURES / endpoint).glob("*.json")
    return path.read_bytes()


def cdn_game_url(endpoint: str, game_id: str) -> str:
    return f"{nba_cdn.CDN_BASE}/liveData/{endpoint}/{endpoint}_{game_id}.json"


def nba_routes(schedule: dict[str, Any] | None = SCHEDULE, *game_ids: str) -> dict[str, bytes]:
    """The schedule, every stats.nba.com per-game endpoint, and the cdn.nba.com per-game feeds of
    `game_ids`; `None` leaves the schedule unrouted, so fetching it fails."""
    routes = {
        f"{nba_stats.STATS_BASE}/{endpoint}": fixture_body(endpoint)
        for endpoint in nba_stats.GAME_ENDPOINTS
    }
    for game_id in game_ids:
        for endpoint in (nba_cdn.BOXSCORE, nba_cdn.PLAYBYPLAY):
            routes[cdn_game_url(endpoint, game_id)] = _cdn_fixture(endpoint)
    if schedule is not None:
        routes[nba_cdn.SCHEDULE_URL] = json.dumps(schedule).encode()
    return routes


def fetched_games(get: RoutedGet) -> dict[str, set[str]]:
    """The game IDs requested from each source."""
    stats = {params["GameID"] for _, params in get.calls if params and "GameID" in params}
    cdn = {
        url.removesuffix(".json").rsplit("_", 1)[1]
        for url, _ in get.calls
        if url.startswith(f"{nba_cdn.CDN_BASE}/liveData/")
    }
    return {"stats": stats, "cdn": cdn}


def runs(db: sessionmaker[Session]) -> list[JobRun]:
    with db() as session:
        return list(session.scalars(select(JobRun).order_by(JobRun.id)))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


@pytest.fixture
def settings() -> Settings:
    # The jobs get their database from `db`, never from here.
    unused = "postgresql+psycopg://unused@127.0.0.1:1/x"
    return Settings(database_url=unused, tz=ZoneInfo("America/New_York"))


def make_jobs(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    get: RoutedGet,
    heartbeat: Heartbeat,
) -> Jobs:
    def nba() -> NbaSession:
        return NbaSession(Connect(get), RateLimiter(1e6, clock), clock)

    return Jobs(
        settings=settings,
        clock=clock,
        cache=RawCache(tmp_path, clock),
        store=ParquetStore(tmp_path),
        nba=nba,
        http=httpx.Client,
        db=db,
        heartbeat=heartbeat,
    )


@respx.mock
def test_nightly_fetches_exactly_the_final_games_of_the_date_and_records_the_run(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    ok = respx.get(NIGHTLY_URL).respond(200)
    get = RoutedGet(nba_routes(SCHEDULE, FINAL_A, FINAL_B))
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({"nightly": NIGHTLY_URL}, http))

    detail = jobs.nightly(DAY)

    assert fetched_games(get) == {"stats": {FINAL_A, FINAL_B}, "cdn": {FINAL_A, FINAL_B}}
    # The schedule, eight stats.nba.com and two cdn.nba.com requests per game, then the lineups
    # and two teams' on/off for reconciliation.
    assert len(get.calls) == 1 + 2 * 10 + 3
    [run] = runs(db)
    assert (run.job, run.status, run.started_at, run.finished_at) == (
        NIGHTLY,
        "succeeded",
        START,
        clock.now(),
    )
    assert run.detail == detail
    assert detail["games"] == detail["fetched"] == [FINAL_A, FINAL_B]
    # `playbyplayv2`'s recorded `{}` fails it for each game, and the unrouted reconciliation
    # responses fail too, without failing the job.
    teams = sample_teams(DAY)
    assert [(f["endpoint"], f["key"]) for f in detail["failed"]] == [
        ("playbyplayv2", FINAL_A),
        ("playbyplayv2", FINAL_B),
        ("leaguedashlineups", "2026-27_regular-season"),
        *(("leagueplayerondetails", f"{team_id}_2026-27_regular-season") for team_id in teams),
    ]
    assert detail["reconcile"] == {
        "season_type": "regular-season",
        "teams": [by_nba_team_id(team_id).tricode for team_id in teams],
        "skipped": "leaguedashlineups 2026-27_regular-season failed to refresh",
    }
    # Every game is served the fixture game's box scores, which the parse step refuses for them.
    assert detail["parsed"] == []
    assert detail["parse_failed"] == {
        game_id: f"ParseError: cached box scores of {game_id} are for game {GAME_ID}"
        for game_id in (FINAL_A, FINAL_B)
    }
    # The date's three counted games, its season's reference rows (none cached), and no box rows.
    assert detail["loaded"] == {
        "game": 3,
        "player": 0,
        "roster_membership": 0,
        "player_game": 0,
        "matchup": 0,
        "stint": 0,
        "possession": 0,
        "possession_lineup": 0,
    }
    # The date is derived, last of any the season left underived, and every step of it succeeded.
    assert detail["derived"][-1] == DAY.isoformat()
    assert list(detail["derive"]) == [step.name for step in [*WRITES, RECONCILE]]
    assert detail["derive"]["bpm"]["season"] == "2026-27"
    with db() as session:
        logged = session.execute(
            select(LoadLog.step, LoadLog.status).where(
                LoadLog.game_date == DAY, LoadLog.step != LOAD_STEP
            )
        ).all()
    # Every stats.nba.com endpoint answered, so the date's stats fetch succeeded too.
    expected = [(scheduler.STATS_STEP, "succeeded")]
    expected += [(step.name, "succeeded") for step in [*WRITES, RECONCILE]]
    assert sorted(logged) == sorted(expected)
    assert ok.call_count == 1


def test_nightly_logs_a_warning_for_each_reconciliation_row_outside_tolerance(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    on_off = nba_stats.LEAGUE_PLAYER_ON_DETAILS
    get = RoutedGet(
        {
            **nba_routes(SCHEDULE, FINAL_A, FINAL_B),
            f"{nba_stats.STATS_BASE}/{nba_stats.LEAGUE_DASH_LINEUPS}": season_fixture_body(
                nba_stats.LEAGUE_DASH_LINEUPS
            ),
            # GSW's on/off, served for both teams of the sample.
            f"{nba_stats.STATS_BASE}/{on_off}": season_fixture_body(on_off),
        }
    )
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    with caplog.at_level("WARNING", logger="closinglineup.scheduler"):
        detail = jobs.nightly(DAY)

    # No game parses, so no stint is loaded and every row NBA.com has is outside tolerance; each
    # of GSW's 22 players has at least 100 possessions.
    reconciled = detail["reconcile"]
    assert reconciled["on_court"] == 2 * 22
    assert len(reconciled["outside"]) == reconciled["lineups"] + reconciled["on_court"]
    assert all("games 0 vs" in line for line in reconciled["outside"])
    warned = [r for r in caplog.records if r.message == "reconciliation outside tolerance"]
    assert [r.__dict__["check"] for r in warned] == reconciled["outside"]
    assert [run.status for run in runs(db)] == ["succeeded"]


def test_nightly_skips_reconciliation_when_nba_com_does_not_answer_its_refresh(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*args: Any, **kwargs: Any) -> list[EndpointResult]:
        raise NbaUnreachableError("no answer")

    monkeypatch.setattr(scheduler, "fetch_reconciliation", unreachable)
    # A copy cached earlier, which covers fewer games than tonight's stints.
    cache = RawCache(tmp_path, clock)
    lineups = nba_stats.LEAGUE_DASH_LINEUPS
    body = season_fixture_body(lineups)
    cache.put(nba_stats.SOURCE, lineups, "2026-27_regular-season", body, url="u", status=200)
    get = RoutedGet(nba_routes(SCHEDULE, FINAL_A, FINAL_B))
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    detail = jobs.nightly(DAY)

    # The date still loads, and the stale copy is not compared against.
    assert detail["loaded"]["game"] == 3
    assert detail["failed"][-1] == {
        "endpoint": "reconcile",
        "key": "2026-10-20",
        "error": "no answer",
    }
    assert detail["reconcile"]["skipped"] == "reconcile 2026-10-20 failed to refresh"
    assert [run.status for run in runs(db)] == ["succeeded"]


def test_nightly_loads_again_an_earlier_date_with_no_successful_load(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    earlier = DAY - timedelta(days=3)
    with db.begin() as session:
        session.execute(delete(LoadLog).where(LoadLog.game_date == earlier))
    store = ParquetStore(tmp_path)
    for table in ROW_MODELS:
        store.write_rows(table, earlier, [])
    get = RoutedGet(nba_routes(SCHEDULE, FINAL_A, FINAL_B))
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    first = jobs.nightly(DAY)
    second = jobs.nightly(DAY)

    assert first["healed"] == [f"{table} {earlier}" for table in ROW_MODELS]
    assert second["healed"] == []


@respx.mock
def test_nightly_with_a_table_that_fails_to_load_fails_the_job(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail = respx.post(f"{NIGHTLY_URL}/fail").respond(200)
    failed = LoadResult("game", DAY, RunStatus.FAILED, error="IntegrityError: boom")

    def failing_load(*args: Any, missing: bool = False) -> list[LoadResult]:
        # Fails the date's own load; there is no earlier date to heal.
        return [] if missing else [failed]

    monkeypatch.setattr(scheduler, "load_dates", failing_load)
    get = RoutedGet(nba_routes(SCHEDULE, FINAL_A, FINAL_B))
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({"nightly": NIGHTLY_URL}, http))

    with pytest.raises(LoadError):
        jobs.nightly(DAY)

    [run] = runs(db)
    reason = "LoadError: load failed: game 2026-10-20: IntegrityError: boom"
    assert (run.status, run.detail) == ("failed", {"error": reason})
    assert fail.call_count == 1


@respx.mock
def test_nightly_that_raises_records_the_run_failed_and_pings_fail(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    ok = respx.get(NIGHTLY_URL).respond(200)
    fail = respx.post(f"{NIGHTLY_URL}/fail").respond(200)
    get = RoutedGet(nba_routes(None))
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({"nightly": NIGHTLY_URL}, http))

    with pytest.raises(NbaHttpError):
        jobs.nightly(DAY)

    [run] = runs(db)
    reason = f"NbaHttpError: HTTP 404 from {nba_cdn.SCHEDULE_URL}"
    assert (run.job, run.status, run.finished_at) == (NIGHTLY, "failed", START)
    assert run.detail == {"error": reason}
    assert fail.call_count == 1
    assert fail.calls.last.request.content == reason.encode()
    assert ok.call_count == 0


def _stats_routes() -> dict[str, bytes]:
    return {
        f"{nba_stats.STATS_BASE}/{endpoint}": fixture_body(endpoint)
        for endpoint in nba_stats.GAME_ENDPOINTS
    }


def _sources(db: sessionmaker[Session], game_id: str) -> list[str]:
    with db() as session:
        return list(session.scalars(select(PlayerGame.source).where(PlayerGame.game_id == game_id)))


def _stats_fetch_log(db: sessionmaker[Session], day: date) -> tuple[str, Any]:
    with db() as session:
        log = session.get_one(LoadLog, (scheduler.STATS_TABLE, day, scheduler.STATS_STEP))
        return log.status, log.detail


def test_a_game_stats_nba_com_fails_for_loads_from_cdn_until_fetch_missing_replaces_it(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    clear_fixture_game(db)
    # The stats.nba.com fixture game alone, final on its date.
    schedule = copy.deepcopy(FIXTURE_SCHEDULE)
    [game_date] = schedule["leagueSchedule"]["gameDates"]
    game_date["games"] = [g for g in game_date["games"] if g["gameId"] == FIXTURE_GAME]
    # cdn.nba.com answers; every stats.nba.com request is a 404.
    get = RoutedGet(
        {
            nba_cdn.SCHEDULE_URL: json.dumps(schedule).encode(),
            **{
                cdn_game_url(endpoint, FIXTURE_GAME): (
                    PBP_FIXTURES / endpoint / f"{FIXTURE_GAME}.json"
                ).read_bytes()
                for endpoint in (nba_cdn.BOXSCORE, nba_cdn.PLAYBYPLAY)
            },
        }
    )
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    detail = jobs.nightly(FIXTURE_DAY)

    [run] = runs(db)
    assert (run.job, run.status) == (NIGHTLY, "succeeded")
    assert detail["stats_failed"] == [FIXTURE_GAME]
    assert detail["parse_fallback"] == detail["parsed"] == [FIXTURE_GAME]
    [warning] = detail["warnings"]
    assert warning.startswith(f"{FIXTURE_GAME}: stats.nba.com failed for boxscoretraditionalv3")
    assert warning.endswith("; box score from cdn.nba.com")
    assert _sources(db, FIXTURE_GAME) == ["cdn"] * PLAYER_GAMES
    status, logged = _stats_fetch_log(db, FIXTURE_DAY)
    assert status == "failed"
    assert list(logged[scheduler.STATS_FAILED]) == [FIXTURE_GAME]
    assert scheduler.stats_failed_games(db) == {FIXTURE_DAY: [FIXTURE_GAME]}

    # stats.nba.com answers again.
    get.routes.update(_stats_routes())
    healed = jobs.fetch_missing()

    assert (healed["dates"], healed["games"]) == ([FIXTURE_DAY.isoformat()], [FIXTURE_GAME])
    assert (healed["still_failed"], healed["fallback"]) == ({}, [])
    assert _sources(db, FIXTURE_GAME) == ["stats"] * PLAYER_GAMES
    assert _stats_fetch_log(db, FIXTURE_DAY) == ("succeeded", {scheduler.STATS_FAILED: {}})
    assert scheduler.stats_failed_games(db) == {}
    assert [(r.job, r.status) for r in runs(db)] == [
        (NIGHTLY, "succeeded"),
        (scheduler.FETCH_MISSING, "succeeded"),
    ]


class StatsDownGet(RoutedGet):
    """`RoutedGet` with stats.nba.com never answering: every request to it times out."""

    def __call__(self, url: str, params: dict[str, str] | None = None) -> Any:
        if url.startswith(nba_stats.STATS_BASE):
            self.calls.append((url, params))
            raise curl_requests.exceptions.Timeout("Operation timed out")
        return super().__call__(url, params)


def test_nightly_with_stats_nba_com_not_answering_loads_every_game_from_cdn(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    clear_fixture_game(db)
    schedule = copy.deepcopy(FIXTURE_SCHEDULE)
    [game_date] = schedule["leagueSchedule"]["gameDates"]
    game_date["games"] = [g for g in game_date["games"] if g["gameId"] == FIXTURE_GAME]
    get = StatsDownGet(
        {
            nba_cdn.SCHEDULE_URL: json.dumps(schedule).encode(),
            **{
                cdn_game_url(endpoint, FIXTURE_GAME): (
                    PBP_FIXTURES / endpoint / f"{FIXTURE_GAME}.json"
                ).read_bytes()
                for endpoint in (nba_cdn.BOXSCORE, nba_cdn.PLAYBYPLAY)
            },
        }
    )
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    detail = jobs.nightly(FIXTURE_DAY)

    [run] = runs(db)
    assert (run.job, run.status) == (NIGHTLY, "succeeded")
    assert detail["stats_failed"] == detail["parse_fallback"] == [FIXTURE_GAME]
    assert [(f["endpoint"], f["key"]) for f in detail["failed"]] == [
        (scheduler.STATS_UNREACHABLE, FIXTURE_GAME),
        ("reconcile", FIXTURE_DAY.isoformat()),
    ]
    # The first endpoint's tries, and none after: reconciliation is not refreshed either.
    stats_calls = [url for url, _ in get.calls if url.startswith(nba_stats.STATS_BASE)]
    assert len(stats_calls) == 1 + nba_http.RETRIES
    assert _sources(db, FIXTURE_GAME) == ["cdn"] * PLAYER_GAMES


def test_nightly_skips_a_game_already_fully_cached(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    cache = RawCache(tmp_path, clock)
    # Everything but `playbyplayv2`, which is never cached.
    for endpoint in nba_stats.GAME_ENDPOINTS:
        if endpoint != nba_stats.PLAYBYPLAY_V2:
            cache.put(nba_stats.SOURCE, endpoint, FINAL_A, b"{}", url="u", status=200)
    for endpoint in (nba_cdn.BOXSCORE, nba_cdn.PLAYBYPLAY):
        cache.put(nba_cdn.SOURCE, endpoint, FINAL_A, b"{}", url="u", status=200)
    get = RoutedGet(nba_routes(SCHEDULE, FINAL_B))
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    detail = jobs.nightly(DAY)

    assert fetched_games(get) == {"stats": {FINAL_B}, "cdn": {FINAL_B}}
    assert (detail["games"], detail["fetched"]) == ([FINAL_A, FINAL_B], [FINAL_B])


def test_nightly_also_fetches_the_games_poll_finals_saw_go_final(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    scoreboard = {
        "scoreboard": {
            "gameDate": "2026-10-20",
            "games": [_game(SCHEDULED, 3), _game("0022600005", 2)],
        }
    }
    get = RoutedGet(
        {
            **nba_routes(SCHEDULE, FINAL_A, FINAL_B, SCHEDULED),
            nba_cdn.SCOREBOARD_URL: json.dumps(scoreboard).encode(),
        }
    )
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    polled = jobs.poll_finals()
    detail = jobs.nightly(DAY)

    assert polled == {"date": "2026-10-20", "finals": [SCHEDULED]}
    assert detail["fetched"] == [FINAL_A, FINAL_B, SCHEDULED]
    assert jobs.finals == {}
    assert [(run.job, run.status) for run in runs(db)] == [
        (POLL_FINALS, "succeeded"),
        (NIGHTLY, "succeeded"),
    ]


def test_scheduled_nightly_runs_for_yesterday_in_tz(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    get = RoutedGet(nba_routes(SCHEDULE, FINAL_A, FINAL_B))
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({}, http))

    detail = jobs.nightly_yesterday()

    assert detail["date"] == "2026-10-20"


def _fire_times(trigger: Any, start: datetime, end: datetime) -> list[datetime]:
    """Every time `trigger` fires in [start, end)."""
    times: list[datetime] = []
    previous: datetime | None = None
    now = start
    while (when := trigger.get_next_fire_time(previous, now)) is not None and when < end:
        times.append(when)
        previous = now = when
    return times


def test_worker_registers_every_job_with_the_spec_triggers(
    settings: Settings, clock: FakeClock, tmp_path: Path, http: httpx.Client
) -> None:
    jobs = make_jobs(settings, clock, tmp_path, sessionmaker(), RoutedGet({}), Heartbeat({}, http))

    scheduler = build_scheduler(jobs)

    scheduled = cast(list[Any], scheduler.get_jobs())  # pyright: ignore[reportUnknownMemberType]
    registered = {job.id: job for job in scheduled}
    assert sorted(registered) == sorted([NIGHTLY, POLL_FINALS, WEEKLY, INJURIES, DAILY_OVERLAYS])
    for job in registered.values():
        assert (job.max_instances, job.coalesce, job.misfire_grace_time) == (1, True, None)
    tz = settings.tz
    noon = datetime(2026, 10, 21, 12, tzinfo=tz)
    day_after = noon + timedelta(days=1)
    assert _fire_times(registered[NIGHTLY].trigger, noon, day_after) == [
        datetime(2026, 10, 22, 3, 30, tzinfo=tz)
    ]
    evening = [datetime(2026, 10, 21, h, m, tzinfo=tz) for h in range(18, 24) for m in (0, 30)]
    night = [datetime(2026, 10, 22, h, m, tzinfo=tz) for h in range(2) for m in (0, 30)]
    assert _fire_times(registered[POLL_FINALS].trigger, noon, day_after) == [
        *evening,
        *night,
        datetime(2026, 10, 22, 2, 0, tzinfo=tz),
    ]
    fortnight = noon + timedelta(days=14)
    # Mondays, the second after the end of daylight saving time.
    assert _fire_times(registered[WEEKLY].trigger, noon, fortnight) == [
        datetime(2026, 10, 26, 4, 30, tzinfo=tz),
        datetime(2026, 11, 2, 4, 30, tzinfo=tz),
    ]
    midnight = datetime(2026, 10, 22, tzinfo=tz)
    next_midnight = midnight + timedelta(days=1)
    assert _fire_times(registered[INJURIES].trigger, midnight, next_midnight) == [
        datetime(2026, 10, 22, h, m, tzinfo=tz) for h, m in [(10, 0), (13, 0), (16, 0), (18, 30)]
    ]
    assert _fire_times(registered[DAILY_OVERLAYS].trigger, midnight, next_midnight) == [
        datetime(2026, 10, 22, 9, 0, tzinfo=tz)
    ]


WEEKLY_URL = "https://hc-ping.com/weekly-123"
SHOT_CHART_URL = f"{nba_stats.STATS_BASE}/{nba_stats.SHOT_CHART_DETAIL}"
# 2026-27 games of each phase, which the weekly tests seed with who played in them.
REGULAR = "0022600091"
CUP_KNOCKOUT = "0022600092"
PLAY_IN = "0052600001"
PLAYOFF = "0042600001"
PHASES = {REGULAR: "regular", CUP_KNOCKOUT: "cup_knockout", PLAY_IN: "play_in", PLAYOFF: "playoff"}
CURRY = 201939


def _seed_played(db: sessionmaker[Session], played: list[tuple[str, int, int]]) -> None:
    """The `PHASES` games, and a GSW `player_game` row per (game, player, seconds) in `played`."""
    games = list(PHASES)
    with db.begin() as session:
        session.execute(delete(Shot).where(Shot.game_id.in_(games)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(games)))
        session.execute(delete(Game).where(Game.game_id.in_(games)))
        for game_id, phase in PHASES.items():
            session.add(
                Game(
                    game_id=game_id,
                    season="2026-27",
                    game_date=DAY,
                    phase=phase,
                    home_team_id=1610612744,
                    away_team_id=1610612743,
                    status="final",
                    home_b2b=False,
                    away_b2b=False,
                )
            )
        session.flush()
        counts = dict.fromkeys(
            ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl"), 0
        )
        for game_id, player_id, seconds in played:
            session.add(
                PlayerGame(
                    game_id=game_id,
                    player_id=player_id,
                    team_id=1610612744,
                    is_starter=False,
                    seconds=seconds,
                    blk=0,
                    tov=0,
                    pf=0,
                    plus_minus=0,
                    source="stats",
                    **counts,
                )
            )


def test_shot_charts_are_fetched_for_each_season_type_a_player_has_played_in(
    db: sessionmaker[Session],
) -> None:
    _seed_played(
        db,
        [
            (REGULAR, 1, 600),
            (CUP_KNOCKOUT, 1, 600),
            (PLAYOFF, 1, 900),
            (CUP_KNOCKOUT, 2, 300),
            (PLAY_IN, 2, 120),
            # A DNP has no shots.
            (REGULAR, 3, 0),
        ],
    )

    found = [(p, t) for p, t in shot_chart_players(db, "2026-27") if p in {1, 2, 3}]

    assert found == [
        (1, SeasonType.PLAYOFFS),
        (1, SeasonType.REGULAR_SEASON),
        (2, SeasonType.PLAY_IN),
        (2, SeasonType.REGULAR_SEASON),
    ]
    assert shot_chart_players(db, "2027-28") == []


def _shot_chart_of(game_id: str) -> bytes:
    """The shot chart fixture, its shots moved to `game_id`."""
    payload = json.loads(season_fixture_body(nba_stats.SHOT_CHART_DETAIL))
    shots = payload["resultSets"][0]
    column = shots["headers"].index("GAME_ID")
    for row in shots["rowSet"]:
        row[column] = game_id
    return json.dumps(payload).encode()


@respx.mock
def test_weekly_refetches_parses_and_loads_the_seasons_shot_charts(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    _seed_played(db, [(REGULAR, CURRY, 2000)])
    ok = respx.get(WEEKLY_URL).respond(200)
    get = RoutedGet({SHOT_CHART_URL: _shot_chart_of(REGULAR)})
    jobs = make_jobs(settings, clock, tmp_path, db, get, Heartbeat({"weekly": WEEKLY_URL}, http))
    players = len(shot_chart_players(db, "2026-27"))

    first = jobs.weekly()
    second = jobs.weekly()

    key = f"{CURRY}_2026-27_regular-season"
    curry = [params for _, params in get.calls if params and params["PlayerID"] == str(CURRY)]
    # Re-fetched each week, since the cache would otherwise keep the first week's shots.
    assert [(p["Season"], p["SeasonType"]) for p in curry] == [("2026-27", "Regular Season")] * 2
    shots = first["ran"]["shots"]
    assert shots["season"] == "2026-27"
    assert shots["shot_charts"] == shots["fetched"] == players
    assert (shots["failed"], shots["parse_failed"]) == ([], {})
    assert (second["ran"]["shots"]["changed"], second["ran"]["shots"]["removed"]) == (0, 0)
    assert key in jobs.store.keys("shot")
    with db() as session:
        loaded = session.scalars(select(Shot).where(Shot.game_id == REGULAR)).all()
        assert {shot.player_id for shot in loaded} == {CURRY}
        assert len(loaded) == 25
    assert [(run.job, run.status) for run in runs(db)] == [(WEEKLY, "succeeded")] * 2
    assert ok.call_count == 2


@respx.mock
def test_weekly_fails_when_every_shot_chart_fails_to_fetch_and_still_runs_its_overlays(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    registry: dict[str, Overlay],
) -> None:
    _seed_played(db, [(REGULAR, CURRY, 2000)])
    contracts = FakeOverlay()
    register_overlay("contracts", "weekly", contracts)
    fail = respx.post(f"{WEEKLY_URL}/fail").respond(200)
    jobs = make_jobs(
        settings, clock, tmp_path, db, RoutedGet({}), Heartbeat({"weekly": WEEKLY_URL}, http)
    )

    with pytest.raises(JobFailedError, match=r"shots: NbaHttpError: .* shot charts failed"):
        jobs.weekly()

    [run] = runs(db)
    assert (run.job, run.status) == (WEEKLY, "failed")
    assert run.detail is not None
    assert run.detail["ran"] == {"contracts": {"rows": 3}}
    assert list(run.detail["failed"]) == ["shots"]
    # Today in New York, 03:30 on 2026-10-21.
    assert contracts.calls == [(jobs, date(2026, 10, 21))]
    assert fail.call_count == 1


DAILY_URL = "https://hc-ping.com/daily-123"


@respx.mock
def test_daily_overlays_runs_each_daily_overlay_once_and_pings_daily(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    registry: dict[str, Overlay],
) -> None:
    transactions, contracts, injury_report = FakeOverlay(), FakeOverlay(), FakeOverlay()
    register_overlay("transactions", "daily", transactions)
    register_overlay("contracts", "weekly", contracts)
    register_overlay("injury_report", "injuries", injury_report)
    ok = respx.get(DAILY_URL).respond(200)
    jobs = make_jobs(
        settings, clock, tmp_path, db, RoutedGet({}), Heartbeat({"daily": DAILY_URL}, http)
    )

    detail = jobs.daily_overlays(DAY)

    assert transactions.calls == [(jobs, DAY)]
    assert contracts.calls == injury_report.calls == []
    assert detail == {"date": "2026-10-20", "ran": {"transactions": {"rows": 3}}, "failed": {}}
    [run] = runs(db)
    assert (run.job, run.status, run.detail) == (DAILY_OVERLAYS, "succeeded", detail)
    assert ok.call_count == 1


@respx.mock
def test_an_overlay_that_raises_fails_the_job_without_stopping_the_others(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    registry: dict[str, Overlay],
) -> None:
    epm, darko = FakeOverlay(RuntimeError("table moved")), FakeOverlay()
    register_overlay("epm", "daily", epm)
    register_overlay("darko", "daily", darko)
    ok = respx.get(DAILY_URL).respond(200)
    fail = respx.post(f"{DAILY_URL}/fail").respond(200)
    jobs = make_jobs(
        settings, clock, tmp_path, db, RoutedGet({}), Heartbeat({"daily": DAILY_URL}, http)
    )

    with pytest.raises(JobFailedError):
        jobs.daily_overlays(DAY)

    assert len(epm.calls) == len(darko.calls) == 1
    reason = "1 of 2 failed: epm: RuntimeError: table moved"
    [run] = runs(db)
    assert (run.job, run.status) == (DAILY_OVERLAYS, "failed")
    assert run.detail == {
        "error": reason,
        "date": "2026-10-20",
        "ran": {"darko": {"rows": 3}},
        "failed": {"epm": "RuntimeError: table moved"},
    }
    assert fail.call_count == 1
    assert fail.calls.last.request.content == reason.encode()
    assert ok.call_count == 0


@respx.mock
def test_only_the_last_injuries_run_of_the_day_pings_daily(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    registry: dict[str, Overlay],
) -> None:
    injury_report = FakeOverlay()
    register_overlay("injury_report", "injuries", injury_report)
    ok = respx.get(DAILY_URL).respond(200)
    jobs = make_jobs(
        settings, clock, tmp_path, db, RoutedGet({}), Heartbeat({"daily": DAILY_URL}, http)
    )
    tz = settings.tz

    pings: list[int] = []
    for hour, minute in [(10, 0), (13, 0), (16, 0), (18, 30)]:
        clock.advance(datetime(2026, 10, 21, hour, minute, tzinfo=tz) - clock.now())
        jobs.injuries_now()
        pings.append(ok.call_count)

    assert pings == [0, 0, 0, 1]
    assert [day for _, day in injury_report.calls] == [date(2026, 10, 21)] * 4
    assert [(run.job, run.status) for run in runs(db)] == [(INJURIES, "succeeded")] * 4


@respx.mock
def test_an_earlier_injuries_run_that_fails_pings_fail(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    registry: dict[str, Overlay],
) -> None:
    register_overlay("injury_report", "injuries", FakeOverlay(RuntimeError("no report")))
    fail = respx.post(f"{DAILY_URL}/fail").respond(200)
    jobs = make_jobs(
        settings, clock, tmp_path, db, RoutedGet({}), Heartbeat({"daily": DAILY_URL}, http)
    )

    with pytest.raises(JobFailedError):
        jobs.injuries(DAY, last=False)

    assert fail.call_count == 1
    assert [(run.job, run.status) for run in runs(db)] == [(INJURIES, "failed")]


@respx.mock
def test_overlay_runs_one_overlay_by_name_with_no_heartbeat(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
    registry: dict[str, Overlay],
) -> None:
    transactions, epm = FakeOverlay(), FakeOverlay()
    register_overlay("transactions", "daily", transactions)
    register_overlay("epm", "daily", epm)
    ok = respx.get(DAILY_URL).respond(200)
    jobs = make_jobs(
        settings, clock, tmp_path, db, RoutedGet({}), Heartbeat({"daily": DAILY_URL}, http)
    )

    detail = jobs.overlay("epm", DAY)
    with pytest.raises(UnknownOverlayError):
        jobs.overlay("darko", DAY)

    assert (detail, epm.calls, transactions.calls) == ({"rows": 3}, [(jobs, DAY)], [])
    assert [(run.job, run.status) for run in runs(db)] == [("overlay:epm", "succeeded")]
    assert ok.call_count == 0

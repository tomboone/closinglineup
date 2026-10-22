import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup import scheduler
from closinglineup.clock import FakeClock
from closinglineup.db.models import Game, JobRun, LoadLog, PlayerGame, RunStatus, Shot
from closinglineup.heartbeat import Heartbeat
from closinglineup.load.loader import LoadError, LoadResult
from closinglineup.load.parquet import ROW_MODELS, ParquetStore
from closinglineup.scheduler import (
    NIGHTLY,
    POLL_FINALS,
    WEEKLY,
    Jobs,
    build_scheduler,
    shot_chart_players,
)
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, RateLimiter
from closinglineup.sources.nba_stats import SeasonType
from tests.fakes import Connect, RoutedGet
from tests.test_nba_stats import GAME_ID, fixture_body, season_fixture_body

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
    # The schedule, then eight stats.nba.com and two cdn.nba.com requests per game.
    assert len(get.calls) == 1 + 2 * 10
    [run] = runs(db)
    assert (run.job, run.status, run.started_at, run.finished_at) == (
        NIGHTLY,
        "succeeded",
        START,
        clock.now(),
    )
    assert run.detail == detail
    assert detail["games"] == detail["fetched"] == [FINAL_A, FINAL_B]
    # `playbyplayv2`'s recorded `{}` fails it for each game, without failing the job.
    assert [(f["endpoint"], f["key"]) for f in detail["failed"]] == [
        ("playbyplayv2", FINAL_A),
        ("playbyplayv2", FINAL_B),
    ]
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
    }
    assert ok.call_count == 1


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
    assert sorted(registered) == [NIGHTLY, POLL_FINALS, WEEKLY]
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
    assert first["season"] == "2026-27"
    assert first["shot_charts"] == first["fetched"] == players
    assert (first["failed"], first["parse_failed"]) == ([], {})
    assert (second["changed"], second["removed"]) == (0, 0)
    assert key in jobs.store.keys("shot")
    with db() as session:
        loaded = session.scalars(select(Shot).where(Shot.game_id == REGULAR)).all()
        assert {shot.player_id for shot in loaded} == {CURRY}
        assert len(loaded) == 25
    assert [(run.job, run.status) for run in runs(db)] == [(WEEKLY, "succeeded")] * 2
    assert ok.call_count == 2


@respx.mock
def test_weekly_fails_when_every_shot_chart_fails_to_fetch(
    settings: Settings,
    clock: FakeClock,
    tmp_path: Path,
    db: sessionmaker[Session],
    http: httpx.Client,
) -> None:
    _seed_played(db, [(REGULAR, CURRY, 2000)])
    fail = respx.post(f"{WEEKLY_URL}/fail").respond(200)
    jobs = make_jobs(
        settings, clock, tmp_path, db, RoutedGet({}), Heartbeat({"weekly": WEEKLY_URL}, http)
    )

    with pytest.raises(NbaHttpError, match="shot charts failed to fetch"):
        jobs.weekly()

    [run] = runs(db)
    assert (run.job, run.status) == (WEEKLY, "failed")
    assert fail.call_count == 1

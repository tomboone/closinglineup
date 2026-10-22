import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from curl_cffi import requests

from closinglineup.clock import FakeClock
from closinglineup.sources import nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, RateLimiter
from closinglineup.sources.nba_stats import EndpointResult, FetchStatus
from tests.fakes import ScriptedGet, fake_response

FIXTURES = Path(__file__).parent / "fixtures" / "nba_stats"
# DEN at GSW, 2025-10-23, one overtime: the game `record_fixtures.py stats` records.
GAME_ID = "0022500006"
START = datetime(2026, 10, 21, 7, 30, tzinfo=UTC)

# Spec §4.1, written out rather than read from the module, so a changed parameter fails here.
BOX = {
    "GameID": GAME_ID,
    "StartPeriod": "0",
    "EndPeriod": "14",
    "StartRange": "0",
    "EndRange": "0",
    "RangeType": "0",
}
BOX_V2 = {**BOX, "EndRange": "55800", "RangeType": "2"}
PBP = {"GameID": GAME_ID, "StartPeriod": "0", "EndPeriod": "14"}
GAME_ONLY = {"GameID": GAME_ID}
EXPECTED_REQUESTS = [
    ("boxscoretraditionalv3", BOX),
    ("boxscoretraditionalv2", BOX_V2),
    ("boxscoreadvancedv3", BOX),
    ("boxscorehustlev2", GAME_ONLY),
    ("boxscorematchupsv3", GAME_ONLY),
    ("boxscoreusagev3", BOX),
    ("playbyplayv3", PBP),
    ("playbyplayv2", PBP),
]
ENDPOINTS = [endpoint for endpoint, _ in EXPECTED_REQUESTS]
# Every endpoint but `playbyplayv2`, whose recorded response is the `{}` it now always returns.
WITH_DATA = [endpoint for endpoint in ENDPOINTS if endpoint != "playbyplayv2"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def session(clock: FakeClock) -> Iterator[NbaSession]:
    with NbaSession(requests.Session(), RateLimiter(1e6, clock), clock) as s:
        yield s


@pytest.fixture
def cache(tmp_path: Path, clock: FakeClock) -> RawCache:
    return RawCache(tmp_path, clock)


def fixture_body(endpoint: str) -> bytes:
    return (FIXTURES / endpoint / f"{GAME_ID}.json").read_bytes()


def _script(session: NbaSession, monkeypatch: pytest.MonkeyPatch, *bodies: bytes) -> ScriptedGet:
    get = ScriptedGet(*(fake_response(200, body) for body in bodies))
    monkeypatch.setattr(session.curl, "get", get)
    return get


def test_fetch_game_requests_each_endpoint_in_order_with_the_spec_params(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    get = _script(session, monkeypatch, *map(fixture_body, ENDPOINTS))

    nba_stats.fetch_game(session, cache, GAME_ID)

    assert get.calls == [
        (f"https://stats.nba.com/stats/{endpoint}", params)
        for endpoint, params in EXPECTED_REQUESTS
    ]


@pytest.mark.parametrize("endpoint", WITH_DATA)
def test_fetch_returns_the_json_and_caches_it_under_the_game_id(
    session: NbaSession,
    cache: RawCache,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    endpoint: str,
) -> None:
    body = fixture_body(endpoint)
    _script(session, monkeypatch, body)

    assert nba_stats.GAME_ENDPOINTS[endpoint](session, cache, GAME_ID) == json.loads(body)
    assert (tmp_path / "raw" / "nba_stats" / endpoint / f"{GAME_ID}.json").read_bytes() == body


def _v3_box_shell(root: str) -> dict[str, Any]:
    """A v3 box score as stats.nba.com returns it for a game not yet played."""
    team: dict[str, Any] = {"teamId": 0, "players": []}
    return {"meta": {}, root: {"gameId": GAME_ID, "homeTeam": team, "awayTeam": team}}


def _v2_shell() -> dict[str, Any]:
    return {"resultSets": [{"name": "PlayerStats", "headers": ["GAME_ID"], "rowSet": []}]}


EMPTY_SHELLS: dict[str, bytes] = {
    "boxscoretraditionalv3": json.dumps(_v3_box_shell("boxScoreTraditional")).encode(),
    "boxscoretraditionalv2": json.dumps(_v2_shell()).encode(),
    "boxscoreadvancedv3": json.dumps(_v3_box_shell("boxScoreAdvanced")).encode(),
    "boxscorehustlev2": json.dumps(_v3_box_shell("boxScoreHustle")).encode(),
    "boxscorematchupsv3": json.dumps(_v3_box_shell("boxScoreMatchups")).encode(),
    "boxscoreusagev3": json.dumps(_v3_box_shell("boxScoreUsage")).encode(),
    "playbyplayv3": json.dumps({"meta": {}, "game": {"gameId": GAME_ID, "actions": []}}).encode(),
    "playbyplayv2": fixture_body("playbyplayv2"),
}


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_response_with_no_game_data_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    _script(session, monkeypatch, EMPTY_SHELLS[endpoint])

    with pytest.raises(NbaHttpError, match=f"{endpoint} has no data for game {GAME_ID}"):
        nba_stats.GAME_ENDPOINTS[endpoint](session, cache, GAME_ID)
    assert not cache.exists("nba_stats", endpoint, GAME_ID)


def test_the_recorded_play_by_play_covers_overtime() -> None:
    pbp = json.loads(fixture_body("playbyplayv3"))

    assert {action["period"] for action in pbp["game"]["actions"]} == {1, 2, 3, 4, 5}


def test_fetch_game_reports_each_endpoint_and_carries_on_past_a_failure(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(session, monkeypatch, *map(fixture_body, ENDPOINTS))

    results = nba_stats.fetch_game(session, cache, GAME_ID)

    assert results == [EndpointResult(e, FetchStatus.FETCHED) for e in WITH_DATA] + [
        EndpointResult(
            "playbyplayv2", FetchStatus.FAILED, f"playbyplayv2 has no data for game {GAME_ID}"
        )
    ]
    assert [e for e in ENDPOINTS if cache.exists("nba_stats", e, GAME_ID)] == WITH_DATA


def _cache_all(cache: RawCache) -> None:
    """Every endpoint cached, `playbyplayv2` included. A real fetch never caches that one while
    it answers `{}`, so a real cached game still requests it on every run."""
    for endpoint in ENDPOINTS:
        url = f"https://stats.nba.com/stats/{endpoint}"
        cache.put("nba_stats", endpoint, GAME_ID, fixture_body(endpoint), url=url, status=200)


def test_fetch_game_on_a_cached_game_makes_no_requests(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_all(cache)
    get = _script(session, monkeypatch)

    results = nba_stats.fetch_game(session, cache, GAME_ID)

    assert get.calls == []
    assert results == [EndpointResult(e, FetchStatus.CACHED) for e in ENDPOINTS]


def test_fetch_game_with_force_refetches_every_endpoint(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_all(cache)
    get = _script(session, monkeypatch, *map(fixture_body, ENDPOINTS))

    results = nba_stats.fetch_game(session, cache, GAME_ID, force=True)

    assert len(get.calls) == 8
    assert [r.status for r in results] == [FetchStatus.FETCHED] * 7 + [FetchStatus.FAILED]

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from curl_cffi import requests

from closinglineup.clock import FakeClock
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.drift import SchemaDrift, Shape, result_sets, top_level
from closinglineup.sources.nba_http import NbaSession, RateLimiter
from closinglineup.sources.nba_stats import EndpointResult, FetchStatus, MeasureType
from tests.fakes import ScriptedGet, fake_response
from tests.test_nba_stats import GAME_ID, START, WITH_DATA, fixture_body

FIXTURES = Path(__file__).parent / "fixtures"
# Fixtures recorded from endpoints with no declared shape: `playbyplayv2`'s is the `{}` it always
# answers, and the player movement feed is not a stats.nba.com API endpoint.
UNDECLARED = {(nba_stats.SOURCE, nba_stats.PLAYBYPLAY_V2), (nba_stats.SOURCE, "playermovement")}
DASHBOARDS = {nba_stats.LEAGUE_DASH_PLAYER_STATS, nba_stats.LEAGUE_DASH_TEAM_STATS}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def session(clock: FakeClock) -> Iterator[NbaSession]:
    with NbaSession(requests.Session[requests.Response], RateLimiter(1e6, clock), clock) as s:
        yield s


@pytest.fixture
def cache(tmp_path: Path, clock: FakeClock) -> RawCache:
    return RawCache(tmp_path, clock)


def _answer(session: NbaSession, monkeypatch: pytest.MonkeyPatch, *bodies: bytes) -> None:
    get = ScriptedGet(*(fake_response(200, body) for body in bodies))
    monkeypatch.setattr(session.curl, "get", get)


def _declared(source: str, endpoint: str) -> Shape:
    if source == nba_cdn.SOURCE:
        return nba_cdn.SHAPES[endpoint]
    if endpoint in DASHBOARDS:
        # Recorded in the `Advanced` measure type only.
        return nba_stats.DASH_SHAPES[(endpoint, MeasureType.ADVANCED)]
    return nba_stats.SHAPES[endpoint]


# Every recorded response of every endpoint, the cdn.nba.com feeds of the pbpstats games included.
RECORDED = [
    (source, path.parent.name, path)
    for source, directory in [
        (nba_stats.SOURCE, FIXTURES / "nba_stats"),
        (nba_cdn.SOURCE, FIXTURES / "cdn"),
        (nba_cdn.SOURCE, FIXTURES / "pbp"),
    ]
    for path in sorted(directory.glob("*/*.json"))
    if (source, path.parent.name) not in UNDECLARED
]


@pytest.mark.parametrize(
    ("source", "endpoint", "path"), RECORDED, ids=[f"{s}/{e}/{p.name}" for s, e, p in RECORDED]
)
def test_every_recorded_response_is_of_its_endpoints_declared_shape(
    source: str, endpoint: str, path: Path
) -> None:
    assert _declared(source, endpoint).diff(json.loads(path.read_bytes())) == []


def test_every_declared_shape_has_a_recorded_response() -> None:
    recorded = {(source, endpoint) for source, endpoint, _ in RECORDED}

    declared = {
        *((nba_stats.SOURCE, endpoint) for endpoint in nba_stats.SHAPES),
        *((nba_stats.SOURCE, endpoint) for endpoint, _ in nba_stats.DASH_SHAPES),
        *((nba_cdn.SOURCE, endpoint) for endpoint in nba_cdn.SHAPES),
    }
    assert declared == recorded


def _renamed(payload: dict[str, Any], result_set: str, old: str, new: str) -> dict[str, Any]:
    for found in payload["resultSets"]:
        if found["name"] == result_set:
            found["headers"] = [new if header == old else header for header in found["headers"]]
    return payload


def test_a_renamed_column_names_both_the_missing_column_and_its_replacement() -> None:
    payload = json.loads(fixture_body(nba_stats.BOXSCORE_TRADITIONAL_V2))
    shape = nba_stats.SHAPES[nba_stats.BOXSCORE_TRADITIONAL_V2]

    differences = shape.diff(_renamed(payload, "PlayerStats", "MIN", "MINUTES"))

    assert differences == [
        "missing PlayerStats column MIN",
        "unexpected PlayerStats column MINUTES",
    ]


def test_a_column_added_or_moved_alone_is_not_drift() -> None:
    shape = result_sets(PlayerStats="GAME_ID PLAYER_ID MIN")
    payload = {
        "resultSets": [{"name": "PlayerStats", "headers": ["MIN", "PLAYER_ID", "GAME_ID", "X"]}]
    }

    assert shape.diff(payload) == []


def test_a_missing_result_set_or_key_is_drift() -> None:
    shape = result_sets(PlayerStats="GAME_ID", TeamStats="GAME_ID")
    payload = {"resultSets": [{"name": "PlayerStats", "headers": ["GAME_ID"]}, {"name": "Teams"}]}

    assert shape.diff(payload) == ["missing result set TeamStats", "unexpected result set Teams"]
    assert top_level("game").diff({"meta": {}, "gameV2": {}}) == [
        "missing key game",
        "unexpected key gameV2",
        "unexpected key meta",
    ]


def test_a_drifted_response_raises_naming_the_difference_and_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = nba_stats.BOXSCORE_TRADITIONAL_V2
    payload = _renamed(json.loads(fixture_body(endpoint)), "PlayerStats", "MIN", "MINUTES")
    _answer(session, monkeypatch, json.dumps(payload).encode())

    with pytest.raises(SchemaDrift, match="missing PlayerStats column MIN") as raised:
        nba_stats.fetch_boxscore_traditional_v2(session, cache, GAME_ID)

    assert str(raised.value).startswith(f"nba_stats/{endpoint} {GAME_ID}: schema drift: ")
    assert raised.value.differences == [
        "missing PlayerStats column MIN",
        "unexpected PlayerStats column MINUTES",
    ]
    assert not cache.exists(nba_stats.SOURCE, endpoint, GAME_ID)


def test_fetch_game_reports_a_drifted_endpoint_and_fetches_the_rest(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    advanced = json.loads(fixture_body(nba_stats.BOXSCORE_ADVANCED_V3))
    advanced["boxScoreAdvancedV4"] = advanced.pop("boxScoreAdvanced")
    bodies = [
        json.dumps(advanced).encode() if e == nba_stats.BOXSCORE_ADVANCED_V3 else fixture_body(e)
        for e in WITH_DATA
    ]
    _answer(session, monkeypatch, *bodies)

    results = nba_stats.fetch_game(session, cache, GAME_ID, skip={nba_stats.PLAYBYPLAY_V2})

    error = (
        f"nba_stats/boxscoreadvancedv3 {GAME_ID}: schema drift: missing key boxScoreAdvanced; "
        "unexpected key boxScoreAdvancedV4; unexpected key meta"
    )
    assert results == [
        EndpointResult(e, GAME_ID, FetchStatus.DRIFT, error)
        if e == nba_stats.BOXSCORE_ADVANCED_V3
        else EndpointResult(e, GAME_ID, FetchStatus.FETCHED)
        for e in WITH_DATA
    ]

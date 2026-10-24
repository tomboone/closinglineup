import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from curl_cffi import requests

from closinglineup.clock import FakeClock
from closinglineup.season import Phase, phase
from closinglineup.sources import nba_cdn
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, RateLimiter
from tests.fakes import ScriptedGet, fake_response

FIXTURES = Path(__file__).parent / "fixtures" / "cdn"
SEASON = "2026-27"
GAME_ID = "0022600003"
START = datetime(2026, 10, 21, 23, 30, 5, tzinfo=UTC)

type Fetch = Callable[[NbaSession, RawCache], dict[str, Any]]


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


def _fixture(endpoint: str) -> Path:
    """The endpoint's one recorded fixture; `record_fixtures.py cdn` replaces it."""
    (path,) = (FIXTURES / endpoint).glob("*.json")
    return path


def _script(session: NbaSession, monkeypatch: pytest.MonkeyPatch, *bodies: bytes) -> ScriptedGet:
    get = ScriptedGet(*(fake_response(200, body) for body in bodies))
    monkeypatch.setattr(session.curl, "get", get)
    return get


@pytest.mark.parametrize(
    ("fetch", "endpoint", "key", "url"),
    [
        (
            partial(nba_cdn.fetch_schedule, season=SEASON),
            "scheduleleaguev2",
            SEASON,
            "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json",
        ),
        (
            nba_cdn.fetch_todays_scoreboard,
            "scoreboard",
            "20261021T233005Z",
            "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json",
        ),
        (
            partial(nba_cdn.fetch_live_boxscore, game_id=GAME_ID),
            "boxscore",
            GAME_ID,
            f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{GAME_ID}.json",
        ),
        (
            partial(nba_cdn.fetch_live_pbp, game_id=GAME_ID),
            "playbyplay",
            GAME_ID,
            f"https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{GAME_ID}.json",
        ),
    ],
    ids=["schedule", "scoreboard", "boxscore", "playbyplay"],
)
def test_fetch_returns_the_json_and_caches_it_under_its_key(
    session: NbaSession,
    cache: RawCache,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fetch: Fetch,
    endpoint: str,
    key: str,
    url: str,
) -> None:
    body = _fixture(endpoint).read_bytes()
    get = _script(session, monkeypatch, body)

    assert fetch(session, cache) == json.loads(body)
    assert get.calls == [(url, None)]
    assert (tmp_path / "raw" / "cdn" / endpoint / f"{key}.json").read_bytes() == body


def test_each_scoreboard_poll_is_kept_as_its_own_snapshot(
    session: NbaSession, cache: RawCache, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = {"scoreboard": {"poll": 1}}, {"scoreboard": {"poll": 2}}
    _script(session, monkeypatch, json.dumps(first).encode(), json.dumps(second).encode())

    nba_cdn.fetch_todays_scoreboard(session, cache)
    clock.current += timedelta(minutes=30)
    assert nba_cdn.fetch_todays_scoreboard(session, cache) == second

    assert cache.list("cdn", "scoreboard") == ["20261021T233005Z", "20261022T000005Z"]


def test_a_schedule_for_another_season_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(session, monkeypatch, _fixture("scheduleleaguev2").read_bytes())

    with pytest.raises(NbaHttpError, match="holds season 2026-27, not 2027-28"):
        nba_cdn.fetch_schedule(session, cache, "2027-28")
    assert cache.list("cdn", "scheduleleaguev2") == []


def test_a_box_score_of_a_game_in_progress_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = json.loads(_fixture("boxscore").read_bytes())
    box["game"]["gameStatus"] = 2
    _script(session, monkeypatch, json.dumps(box).encode())

    with pytest.raises(NbaHttpError, match="not final"):
        nba_cdn.fetch_live_boxscore(session, cache, GAME_ID)
    assert not cache.exists("cdn", "boxscore", GAME_ID)


def test_play_by_play_without_a_game_end_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    pbp = json.loads(_fixture("playbyplay").read_bytes())
    pbp["game"]["actions"] = [a for a in pbp["game"]["actions"] if a["actionType"] != "game"]
    _script(session, monkeypatch, json.dumps(pbp).encode())

    with pytest.raises(NbaHttpError, match="not final"):
        nba_cdn.fetch_live_pbp(session, cache, GAME_ID)
    assert not cache.exists("cdn", "playbyplay", GAME_ID)


# One recorded game per kind in the trimmed 2026-27 CDN schedule; `None` means never counted. The
# file lists All-Star, play-in and playoff games only once they are scheduled; the 2025-26
# stats.nba.com fixture in `test_season.py`, in the same shape, covers those.
EXPECTED_2026_27: dict[str, Phase | None] = {
    "0012600009": None,  # preseason, Canada Game
    "0012600012": None,  # preseason, China Global Game
    "0022600001": Phase.REGULAR,
    "0022600015": Phase.REGULAR,  # Cup East Group C, subtype `in-season`
    "0022600189": Phase.REGULAR,  # Mexico City Global Game
    "0022601201": Phase.CUP_KNOCKOUT,  # Cup Quarterfinal, a 002 game
    "0062600001": Phase.CUP_KNOCKOUT,  # Cup Championship
    "0022600075": Phase.REGULAR,  # Paris Global Game
    "0022600076": Phase.REGULAR,  # Manchester Global Game
    "0022600673": Phase.REGULAR,  # Rivals Week
    "0022600731": Phase.REGULAR,  # Pioneers Classic
}


@dataclass(frozen=True)
class Game:
    game_id: str
    game_subtype: str


def test_the_schedule_fixture_reads_into_phases() -> None:
    raw: dict[str, Any] = json.loads(_fixture("scheduleleaguev2").read_text())
    games = [
        Game(g["gameId"], g["gameSubtype"])
        for d in raw["leagueSchedule"]["gameDates"]
        for g in d["games"]
    ]

    assert {g.game_id: phase(g) for g in games} == EXPECTED_2026_27

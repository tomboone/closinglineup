import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from curl_cffi import requests

from closinglineup.backfill import PAUSES, SeasonGame, fetch_season
from closinglineup.clock import FakeClock
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import (
    RETRIES,
    NbaHttpError,
    NbaSession,
    NbaUnreachableError,
    RateLimiter,
)
from closinglineup.sources.nba_stats import EndpointResult, FetchStatus
from tests.fakes import RoutedGet
from tests.test_nba_stats import WITH_DATA
from tests.test_scheduler import (
    DAY,
    FINAL_A,
    FINAL_B,
    FINAL_DAY_BEFORE,
    SCHEDULE,
    fetched_games,
    nba_routes,
)

SEASON = "2026-27"
SCHEDULE_URL = f"{nba_stats.STATS_BASE}/{nba_stats.SCHEDULE_LEAGUE_V2}"
CDN = [nba_cdn.BOXSCORE, nba_cdn.PLAYBYPLAY]
# The final counted games of `SCHEDULE`, oldest first: one the day before `DAY`, two on it.
FINALS = [(DAY.replace(day=19), FINAL_DAY_BEFORE), (DAY, FINAL_A), (DAY, FINAL_B)]
# Each final game's requests: every stats.nba.com endpoint but `playbyplayv2`, then both feeds.
PER_GAME = len(WITH_DATA) + len(CDN)


class InterruptedGet(RoutedGet):
    """`RoutedGet`, interrupted as by Ctrl-C when it is asked for request number `at`, or with
    `error` from then on."""

    def __init__(
        self, routes: Mapping[str, bytes], at: int, error: BaseException | None = None
    ) -> None:
        super().__init__(routes)
        self.at = at
        self.error = error

    def __call__(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        if self.error is not None and len(self.calls) + 1 >= self.at:
            self.calls.append((url, params))
            raise self.error
        if len(self.calls) + 1 == self.at:
            raise KeyboardInterrupt
        return super().__call__(url, params)


class ThrottledGet(RoutedGet):
    """`RoutedGet`, timing out on every request whose number (from 1) falls in one of `windows`,
    as stats.nba.com does while it throttles."""

    def __init__(self, routes: Mapping[str, bytes], *windows: range) -> None:
        super().__init__(routes)
        self.windows = windows

    def __call__(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        if any(len(self.calls) + 1 in window for window in self.windows):
            self.calls.append((url, params))
            raise requests.exceptions.Timeout("curl: (28) Operation timed out")
        return super().__call__(url, params)


class Paused(list[tuple[date, str, timedelta]]):
    """An `on_pause` that records each pause."""

    def __call__(self, day: date, game_id: str, wait: timedelta) -> None:
        self.append((day, game_id, wait))


def season_routes() -> dict[str, bytes]:
    """`SCHEDULE` from stats.nba.com, and every per-game endpoint of its final games."""
    routes = nba_routes(None, *(game_id for _, game_id in FINALS))
    routes[SCHEDULE_URL] = json.dumps(SCHEDULE).encode()
    return routes


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 22, 7, 30, tzinfo=UTC))


@pytest.fixture
def cache(tmp_path: Path, clock: FakeClock) -> RawCache:
    return RawCache(tmp_path, clock)


def _session(get: RoutedGet, clock: FakeClock) -> NbaSession:
    session = NbaSession(requests.Session(), RateLimiter(1e6, clock), clock)
    session.curl.get = get  # pyright: ignore[reportAttributeAccessIssue]
    return session


def _throttle(start: int) -> range:
    """The requests, from number `start`, of one throttled spell `NbaSession` gives up on: each
    try of a request."""
    return range(start, start + RETRIES + 1)


def _requested(get: RoutedGet) -> list[str]:
    """Each request's endpoint: the last path segment of a stats.nba.com URL, the feed of a
    cdn.nba.com one."""
    return [
        url.rsplit("/", 1)[1] if url.startswith(nba_stats.STATS_BASE) else url.split("/")[-2]
        for url, _ in get.calls
    ]


def test_fetch_season_fetches_each_final_game_oldest_first_and_caches_the_schedule(
    cache: RawCache, clock: FakeClock
) -> None:
    get = RoutedGet(season_routes())

    games = list(fetch_season(_session(get, clock), cache, clock, SEASON))

    assert [(game.game_date, game.game_id) for game in games] == FINALS
    assert len(get.calls) == 1 + 3 * PER_GAME
    assert _requested(get)[: 1 + PER_GAME] == [nba_stats.SCHEDULE_LEAGUE_V2, *WITH_DATA, *CDN]
    assert all(not game.failed for game in games)
    assert cache.exists(nba_stats.SOURCE, nba_stats.SCHEDULE_LEAGUE_V2, SEASON)


def test_fetch_season_skips_fully_cached_games_without_a_request(
    cache: RawCache, clock: FakeClock
) -> None:
    list(fetch_season(_session(RoutedGet(season_routes()), clock), cache, clock, SEASON))
    get = RoutedGet(season_routes())

    games = list(fetch_season(_session(get, clock), cache, clock, SEASON))

    assert games == [SeasonGame(day, game_id) for day, game_id in FINALS]
    assert _requested(get) == [nba_stats.SCHEDULE_LEAGUE_V2]


def test_fetch_season_stops_at_an_interrupted_game_and_resumes_it_where_it_stopped(
    cache: RawCache, clock: FakeClock
) -> None:
    # The schedule, the first game, then three endpoints of the second before the interruption.
    stopped = 1 + PER_GAME + 3
    first = InterruptedGet(season_routes(), at=stopped + 1)

    done: list[SeasonGame] = []
    with pytest.raises(KeyboardInterrupt):
        done.extend(fetch_season(_session(first, clock), cache, clock, SEASON))
    assert [game.game_id for game in done] == [FINAL_DAY_BEFORE]
    assert len(first.calls) == stopped

    second = RoutedGet(season_routes())
    games = list(fetch_season(_session(second, clock), cache, clock, SEASON))

    # The finished game is skipped; the interrupted one resumes at its first endpoint not cached.
    assert games[0] == SeasonGame(*FINALS[0])
    resumed = WITH_DATA[3:]
    assert _requested(second) == [
        nba_stats.SCHEDULE_LEAGUE_V2,
        *resumed,
        *CDN,
        *WITH_DATA,
        *CDN,
    ]
    assert games[1].results == [
        *(EndpointResult(e, FINAL_A, FetchStatus.CACHED) for e in WITH_DATA[:3]),
        *(EndpointResult(e, FINAL_A, FetchStatus.FETCHED) for e in [*resumed, *CDN]),
    ]
    assert fetched_games(second) == {"stats": {FINAL_A, FINAL_B}, "cdn": {FINAL_A, FINAL_B}}


def test_fetch_season_stops_naming_the_game_when_nba_com_stops_answering_and_resumes_it(
    cache: RawCache, clock: FakeClock
) -> None:
    # The schedule, the first game, then a timeout on every try of the second game's first request.
    timeout = requests.exceptions.Timeout("curl: (28) Operation timed out")
    first = InterruptedGet(season_routes(), at=1 + PER_GAME + 1, error=timeout)

    done: list[SeasonGame] = []
    paused = Paused()
    with pytest.raises(NbaUnreachableError, match=f"game {FINAL_A} of {DAY}: no answer from"):
        done.extend(
            fetch_season(
                _session(first, clock),
                cache,
                clock,
                SEASON,
                on_pause=paused,
            )
        )
    assert [game.game_id for game in done] == [FINAL_DAY_BEFORE]
    # It waited out every pause, trying the game again after each, before it stopped.
    assert paused == [(DAY, FINAL_A, wait) for wait in PAUSES]
    assert len(first.calls) - (1 + PER_GAME) == (len(PAUSES) + 1) * (RETRIES + 1)

    second = RoutedGet(season_routes())
    games = list(fetch_season(_session(second, clock), cache, clock, SEASON))

    assert [game.game_id for game in games] == [FINAL_DAY_BEFORE, FINAL_A, FINAL_B]
    assert fetched_games(second)["stats"] == {FINAL_A, FINAL_B}


def test_fetch_season_waits_out_a_throttle_and_carries_on_with_the_game(
    cache: RawCache, clock: FakeClock
) -> None:
    # The schedule, the first game and three endpoints of the second, then a throttled spell.
    get = ThrottledGet(season_routes(), _throttle(1 + PER_GAME + 4))
    paused = Paused()
    began = clock.now()

    games = list(fetch_season(_session(get, clock), cache, clock, SEASON, on_pause=paused))

    assert [game.game_id for game in games] == [FINAL_DAY_BEFORE, FINAL_A, FINAL_B]
    assert all(not game.failed for game in games)
    assert paused == [(DAY, FINAL_A, PAUSES[0])]
    assert clock.now() - began >= PAUSES[0]
    # The try after the pause picks the game up at the endpoint that went unanswered.
    assert fetched_games(get) == {
        "stats": {FINAL_DAY_BEFORE, FINAL_A, FINAL_B},
        "cdn": {FINAL_DAY_BEFORE, FINAL_A, FINAL_B},
    }
    assert len(get.calls) == 1 + 3 * PER_GAME + RETRIES + 1


def test_a_game_that_gets_through_starts_the_next_stop_at_the_first_pause(
    cache: RawCache, clock: FakeClock
) -> None:
    # A throttled spell in the second game, and another in the third.
    second = 1 + PER_GAME + 1
    third = second + (RETRIES + 1) + PER_GAME
    get = ThrottledGet(season_routes(), _throttle(second), _throttle(third))
    paused = Paused()

    games = list(fetch_season(_session(get, clock), cache, clock, SEASON, on_pause=paused))

    assert len(games) == 3
    assert paused == [(DAY, FINAL_A, PAUSES[0]), (DAY, FINAL_B, PAUSES[0])]


def test_fetch_season_keeps_to_the_dates_from_and_to(cache: RawCache, clock: FakeClock) -> None:
    get = RoutedGet(season_routes())

    games = list(fetch_season(_session(get, clock), cache, clock, SEASON, start=DAY, end=DAY))

    assert [game.game_id for game in games] == [FINAL_A, FINAL_B]
    assert fetched_games(get)["stats"] == {FINAL_A, FINAL_B}


def test_fetch_season_reports_a_feed_that_answers_badly_and_carries_on(
    cache: RawCache, clock: FakeClock
) -> None:
    routes = season_routes()
    del routes[f"{nba_cdn.CDN_BASE}/liveData/boxscore/boxscore_{FINAL_A}.json"]
    get = RoutedGet(routes)

    games = list(fetch_season(_session(get, clock), cache, clock, SEASON, start=DAY))

    assert [(r.endpoint, r.key) for r in games[0].failed] == [(nba_cdn.BOXSCORE, FINAL_A)]
    assert games[1].failed == []


def test_fetch_season_raises_when_the_schedule_cannot_be_fetched(
    cache: RawCache, clock: FakeClock
) -> None:
    routes = season_routes()
    del routes[SCHEDULE_URL]

    with pytest.raises(NbaHttpError, match="HTTP 404"):
        list(fetch_season(_session(RoutedGet(routes), clock), cache, clock, SEASON))


def test_a_date_with_no_finals_fetches_nothing(cache: RawCache, clock: FakeClock) -> None:
    get = RoutedGet(season_routes())
    later = date(2026, 11, 1)

    assert list(fetch_season(_session(get, clock), cache, clock, SEASON, start=later)) == []
    assert _requested(get) == [nba_stats.SCHEDULE_LEAGUE_V2]

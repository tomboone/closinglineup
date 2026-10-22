import random
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from curl_cffi import requests

import closinglineup
from closinglineup.clock import FakeClock
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import (
    NbaHttpError,
    NbaSession,
    NbaUnreachableError,
    RateLimiter,
    fetch_json,
    make_nba_session,
)
from tests.fakes import ScriptedGet, fake_response

URL = "https://stats.nba.com/stats/boxscoretraditionalv3"
PARAMS = {"GameID": "0022500001"}
START = datetime(2026, 10, 21, 3, 30, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def session(clock: FakeClock) -> Iterator[NbaSession]:
    """Rate-limited so loosely that every second of fake time that passes is retry backoff."""
    with NbaSession(requests.Session(), RateLimiter(1e6, clock), clock) as s:
        yield s


def _elapsed(clock: FakeClock) -> float:
    return (clock.now() - START).total_seconds()


def test_nba_http_is_the_only_importer_of_curl_cffi() -> None:
    package = Path(closinglineup.__file__).parent
    importers = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if re.search(r"^\s*(import|from)\s+curl_cffi\b", path.read_text(), re.MULTILINE)
    }

    assert importers == {"sources/nba_http.py"}


def test_session_impersonates_chrome_with_nba_com_headers(clock: FakeClock) -> None:
    settings = Settings(database_url="postgresql://unused", nba_timeout_seconds=12)

    with make_nba_session(settings, clock) as session:
        assert session.curl.impersonate == "chrome"
        assert session.curl.headers["Origin"] == "https://www.nba.com"
        assert session.curl.headers["Referer"] == "https://www.nba.com/"
        assert session.curl.headers["Accept"] == "application/json"
        assert session.curl.timeout == 12


@pytest.mark.parametrize("seed", range(20))
def test_ten_requests_at_two_rps_span_at_least_four_and_a_half_seconds(seed: int) -> None:
    clock = FakeClock(START)
    limiter = RateLimiter(2.0, clock, rng=random.Random(seed))

    sent: list[datetime] = []
    for _ in range(10):
        limiter.acquire()
        sent.append(clock.now())

    assert sent[0] == START
    assert (sent[-1] - sent[0]).total_seconds() >= 4.5
    gaps = [(b - a).total_seconds() for a, b in pairwise(sent)]
    assert all(0.35 - 1e-6 <= gap <= 0.65 + 1e-6 for gap in gaps), gaps


def test_rate_limiter_does_not_wait_after_an_idle_spell(clock: FakeClock) -> None:
    limiter = RateLimiter(2.0, clock, jitter=0)

    limiter.acquire()
    clock.advance(timedelta(minutes=5))
    resumed = clock.now()
    limiter.acquire()

    assert clock.now() == resumed


def test_a_503_then_200_returns_the_200(
    session: NbaSession, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    get = ScriptedGet(fake_response(503), fake_response(200))
    monkeypatch.setattr(session.curl, "get", get)

    response = session.get(URL, params=PARAMS)

    assert response.status_code == 200
    assert get.calls == [(URL, PARAMS)] * 2
    assert _elapsed(clock) == pytest.approx(0.5, abs=1e-3)


def test_gives_up_after_three_retries_and_returns_the_last_5xx(
    session: NbaSession, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    get = ScriptedGet(*[fake_response(502)] * 4)
    monkeypatch.setattr(session.curl, "get", get)

    assert session.get(URL).status_code == 502
    assert len(get.calls) == 4
    assert _elapsed(clock) == pytest.approx(0.5 + 1.0 + 2.0, abs=1e-3)


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectionError("refused"),
        # What curl error 28, an operation timed out, raises: a plain `Timeout`, which is not a
        # `ConnectionError`.
        requests.exceptions.Timeout("curl: (28) Operation timed out"),
        requests.exceptions.ReadTimeout("slow"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_connection_errors_and_timeouts_are_retried_then_raised_as_unreachable(
    session: NbaSession, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    get = ScriptedGet(*[error] * 4)
    monkeypatch.setattr(session.curl, "get", get)

    with pytest.raises(NbaUnreachableError, match=f"no answer from {URL} in 4 tries") as raised:
        session.get(URL)
    assert len(get.calls) == 4
    assert raised.value.__cause__ is error


def test_a_timeout_then_200_returns_the_200(
    session: NbaSession, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    get = ScriptedGet(requests.exceptions.Timeout("timed out"), fake_response(200, b"{}"))
    monkeypatch.setattr(session.curl, "get", get)

    assert session.get(URL).status_code == 200
    assert _elapsed(clock) == pytest.approx(0.5, abs=1e-3)


def test_does_not_retry_a_403(
    session: NbaSession, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    get = ScriptedGet(fake_response(403))
    monkeypatch.setattr(session.curl, "get", get)

    assert session.get(URL).status_code == 403
    assert len(get.calls) == 1
    assert _elapsed(clock) == pytest.approx(0, abs=1e-3)


def _fetch(session: NbaSession, cache: RawCache, *, force: bool = False) -> dict[str, Any]:
    return fetch_json(
        session, cache, "nba_stats", "boxscoretraditionalv3", "0022500001", URL, PARAMS, force=force
    )


def test_fetch_json_miss_fetches_writes_and_returns(
    session: NbaSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    get = ScriptedGet(fake_response(200, b'{"boxScore": 1}'))
    monkeypatch.setattr(session.curl, "get", get)

    assert _fetch(session, cache) == {"boxScore": 1}
    assert cache.get("nba_stats", "boxscoretraditionalv3", "0022500001") == b'{"boxScore": 1}'


def test_fetch_json_hit_returns_the_file_without_a_request(
    session: NbaSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    cache.put(
        "nba_stats", "boxscoretraditionalv3", "0022500001", b'{"cached": true}', url=URL, status=200
    )
    get = ScriptedGet()
    monkeypatch.setattr(session.curl, "get", get)

    assert _fetch(session, cache) == {"cached": True}
    assert get.calls == []


def test_fetch_json_force_refetches_and_overwrites(
    session: NbaSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    cache.put("nba_stats", "boxscoretraditionalv3", "0022500001", b'{"v": 1}', url=URL, status=200)
    monkeypatch.setattr(session.curl, "get", ScriptedGet(fake_response(200, b'{"v": 2}')))

    assert _fetch(session, cache, force=True) == {"v": 2}
    assert cache.get("nba_stats", "boxscoretraditionalv3", "0022500001") == b'{"v": 2}'


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (fake_response(403, b"Access Denied"), "HTTP 403"),
        (fake_response(200, b"<html>maintenance</html>"), "not JSON"),
        (fake_response(200, b"[1, 2]"), "not a JSON object"),
    ],
)
def test_fetch_json_never_caches_a_bad_response(
    session: NbaSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: requests.Response,
    error: str,
) -> None:
    cache = RawCache(tmp_path)
    monkeypatch.setattr(session.curl, "get", ScriptedGet(response))

    with pytest.raises(NbaHttpError, match=error):
        _fetch(session, cache)
    assert not cache.exists("nba_stats", "boxscoretraditionalv3", "0022500001")

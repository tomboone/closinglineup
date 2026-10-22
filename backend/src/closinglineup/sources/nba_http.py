"""The one way sources reach NBA.com: curl_cffi imitating Chrome, with nba.com headers (ADR-0002).

`cdn.nba.com` and `stats.nba.com` sit behind Akamai, which fingerprints the TLS handshake. Probed
from the home IP on 2026-10-20: curl, httpx, and requests got a 403; curl_cffi with
`impersonate="chrome"` and the `Origin`/`Referer` below got a 200; the same without those headers
got a 403 again. Both are required.

Ported from nba-today's `nba_http.py`, adding the rate limiter (spec §4). This is the only module
that imports curl_cffi (spec §3.1). curl_cffi's own `retry` never retries a 5xx, so the retry loop
here is our own. Unlike nba-today's, it retries a timeout too: a backfill runs for hours
unattended, and stats.nba.com now and then holds a request open past `NBA_TIMEOUT_SECONDS` with
nothing sent (curl error 28, seen on kumadrome 2026-10-22), which one more try usually gets past.
"""

import json
import logging
import random
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Self

from curl_cffi import requests

from closinglineup.clock import Clock
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache

IMPERSONATE = "chrome"
NBA_HEADERS = {
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
}
RETRIES = 3
BACKOFF_SECONDS = 0.5
# Each gap between requests is the nominal interval ± this fraction of it (spec §4).
JITTER = 0.3

log = logging.getLogger(__name__)


class NbaHttpError(Exception):
    """A response that must not be cached: a non-200 status, a body that is not a JSON object, or
    one its endpoint's check rejects."""


class RateLimiter:
    """At most `rps` requests per second on average, each gap between them jittered by ±`jitter`.

    Each request gets a slot one interval after the previous request's slot and goes out a random
    0 to `jitter` intervals after it. Slots never drift, so n requests always span at least n - 1
    intervals, while consecutive gaps range over (1 ± `jitter`) intervals. The first request, and
    the first after an idle spell, has nothing to space itself from and goes out at once.
    """

    def __init__(
        self,
        rps: float,
        clock: Clock,
        *,
        jitter: float = JITTER,
        rng: random.Random | None = None,
    ) -> None:
        self._interval = timedelta(seconds=1 / rps)
        self._clock = clock
        self._jitter = jitter
        self._rng = rng or random.Random()
        self._slot: datetime | None = None

    def acquire(self) -> None:
        """Block until the next request may go out."""
        now = self._clock.now()
        if self._slot is None or now >= self._slot + self._interval:
            self._slot = now
            return
        self._slot += self._interval
        self._clock.sleep_until(self._slot + self._interval * (self._jitter * self._rng.random()))


class NbaUnreachableError(Exception):
    """NBA.com did not answer a request on any try: a connection error or timeout on each.

    Not an `NbaHttpError`, which a batch fetch reports per endpoint and carries on past: when
    NBA.com is not answering, the next request will not get an answer either, so a run stops.
    """


class NbaSession:
    """A rate-limited curl_cffi session that retries connection errors, timeouts and 5xx
    responses.

    Every attempt, retries included, waits its turn at the rate limiter. Between tries it sleeps
    `backoff * 2**attempt`. After the last retry a 5xx response is returned as-is, and a
    connection error or timeout raises `NbaUnreachableError`.
    """

    def __init__(
        self,
        curl: requests.Session[requests.Response],
        limiter: RateLimiter,
        clock: Clock,
        *,
        retries: int = RETRIES,
        backoff: float = BACKOFF_SECONDS,
    ) -> None:
        self.curl = curl
        self._limiter = limiter
        self._clock = clock
        self._retries = retries
        self._backoff = backoff

    def get(self, url: str, params: Mapping[str, str] | None = None) -> requests.Response:
        query = dict(params) if params is not None else None
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            self._limiter.acquire()
            try:
                response = self.curl.get(url, params=query)
            # `Timeout` is not a `ConnectionError`: curl error 28, an operation timed out, raises
            # a plain one.
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                reason = type(exc).__name__
                if last:
                    tries = self._retries + 1
                    raise NbaUnreachableError(
                        f"no answer from {url} in {tries} tries: {reason}: {exc}"
                    ) from exc
            else:
                if response.status_code < 500 or last:
                    return response
                reason = str(response.status_code)
            delay = timedelta(seconds=self._backoff * 2**attempt)
            log.warning(
                "retrying request", extra={"url": url, "reason": reason, "attempt": attempt + 1}
            )
            self._clock.sleep_until(self._clock.now() + delay)
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.curl.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def make_nba_session(settings: Settings, clock: Clock) -> NbaSession:
    curl = requests.Session(
        impersonate=IMPERSONATE, headers=NBA_HEADERS, timeout=settings.nba_timeout_seconds
    )
    return NbaSession(curl, RateLimiter(settings.nba_rps, clock), clock)


def fetch_json(
    session: NbaSession,
    cache: RawCache,
    source: str,
    endpoint: str,
    key: str,
    url: str,
    params: Mapping[str, str] | None = None,
    *,
    force: bool = False,
    check: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """The cached response for `key`, fetching and caching it first on a miss or with `force`.

    Only a 200 whose body is a JSON object, and which `check` accepts by returning rather than
    raising `NbaHttpError`, is cached; anything else raises, so a bad response is retried on the
    next run instead of being kept forever.
    """
    if not force and (cached := cache.get(source, endpoint, key)) is not None:
        return _json_object(cached, url)
    response = session.get(url, params)
    if response.status_code != 200:
        raise NbaHttpError(f"HTTP {response.status_code} from {url}")
    data = _json_object(response.content, url)
    if check is not None:
        check(data)
    cache.put(
        source,
        endpoint,
        key,
        response.content,
        url=url,
        status=response.status_code,
        params=params,
        force=force,
    )
    log.info("fetched", extra={"source": source, "endpoint": endpoint, "key": key})
    return data


def _json_object(content: bytes, url: str) -> dict[str, Any]:
    try:
        data: object = json.loads(content)
    except ValueError as exc:
        raise NbaHttpError(f"response from {url} is not JSON") from exc
    if not isinstance(data, dict):
        raise NbaHttpError(f"response from {url} is not a JSON object")
    return data  # pyright: ignore[reportUnknownVariableType]

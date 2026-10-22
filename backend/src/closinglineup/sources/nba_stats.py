"""stats.nba.com endpoint functions, one per endpoint (spec §4.1).

Parameter names follow nba_api's documentation for each endpoint, and every parameter nba_api
fills with a default is passed explicitly. Every function goes through `fetch_json`, so a response
is in the raw cache, under `raw/nba_stats/<endpoint>/<key>.json`, before anything parses it
(spec §5).

stats.nba.com answers a game with nothing in it, such as one not yet played or an endpoint it no
longer serves, with a 200 and an empty shell rather than an error. Each function checks for that
shell and raises `NbaHttpError`, since the cache is write-once and would otherwise keep it in
place of the game's data forever. A game in progress is not detected: only final games are
fetched (spec §3.3).
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, fetch_json

SOURCE = "nba_stats"
STATS_BASE = "https://stats.nba.com/stats"

BOXSCORE_TRADITIONAL_V3 = "boxscoretraditionalv3"
BOXSCORE_TRADITIONAL_V2 = "boxscoretraditionalv2"
BOXSCORE_ADVANCED_V3 = "boxscoreadvancedv3"
BOXSCORE_HUSTLE_V2 = "boxscorehustlev2"
BOXSCORE_MATCHUPS_V3 = "boxscorematchupsv3"
BOXSCORE_USAGE_V3 = "boxscoreusagev3"
PLAYBYPLAY_V3 = "playbyplayv3"
PLAYBYPLAY_V2 = "playbyplayv2"

# Periods 0 to 14 cover any game: 0 means "from the start", and 14 is past the most overtimes
# ever played. A `RangeType` of 0 ignores `StartRange` and `EndRange`.
WHOLE_GAME = {"StartPeriod": "0", "EndPeriod": "14"}
WHOLE_GAME_BOX = {**WHOLE_GAME, "StartRange": "0", "EndRange": "0", "RangeType": "0"}
# `boxscoretraditionalv2` returns no rows for `RangeType` 0, so it takes the elapsed-time range
# pbpstats's own loader sends: 0 to 55800 tenths of a second, which covers nine overtimes.
# `EndRange` 28800 stops at the end of regulation. Probed on 2026-10-21.
WHOLE_GAME_BOX_V2 = {**WHOLE_GAME, "StartRange": "0", "EndRange": "55800", "RangeType": "2"}

type Payload = dict[str, Any]
type Check = Callable[[Payload], None]


def _v3_box_check(endpoint: str, root: str, game_id: str) -> Check:
    """A v3 box score of a game with no data has team ID 0 in place of its home team."""

    def check(data: Payload) -> None:
        if not data.get(root, {}).get("homeTeam", {}).get("teamId"):
            raise NbaHttpError(f"{endpoint} has no data for game {game_id}")

    return check


def _v2_check(endpoint: str, game_id: str) -> Check:
    """A v2 response of a game with no data has an empty first result set, or no result sets."""

    def check(data: Payload) -> None:
        result_sets: list[Payload] = data.get("resultSets") or [{}]
        if not result_sets[0].get("rowSet"):
            raise NbaHttpError(f"{endpoint} has no data for game {game_id}")

    return check


def _fetch_game_endpoint(
    session: NbaSession,
    cache: RawCache,
    endpoint: str,
    game_id: str,
    params: dict[str, str],
    check: Check,
    *,
    force: bool,
) -> Payload:
    url = f"{STATS_BASE}/{endpoint}"
    return fetch_json(
        session,
        cache,
        SOURCE,
        endpoint,
        game_id,
        url,
        {"GameID": game_id, **params},
        force=force,
        check=check,
    )


def fetch_boxscore_traditional_v3(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """Starters, minutes, the traditional box and DNP reasons."""
    endpoint = BOXSCORE_TRADITIONAL_V3
    check = _v3_box_check(endpoint, "boxScoreTraditional", game_id)
    return _fetch_game_endpoint(
        session, cache, endpoint, game_id, WHOLE_GAME_BOX, check, force=force
    )


def fetch_boxscore_traditional_v2(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """The traditional box in the v2 result-set shape, which pbpstats reads as game details."""
    endpoint = BOXSCORE_TRADITIONAL_V2
    check = _v2_check(endpoint, game_id)
    return _fetch_game_endpoint(
        session, cache, endpoint, game_id, WHOLE_GAME_BOX_V2, check, force=force
    )


def fetch_boxscore_advanced_v3(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """TS%, eFG%, usage, AST%, TOV%, REB% and ratings."""
    endpoint = BOXSCORE_ADVANCED_V3
    check = _v3_box_check(endpoint, "boxScoreAdvanced", game_id)
    return _fetch_game_endpoint(
        session, cache, endpoint, game_id, WHOLE_GAME_BOX, check, force=force
    )


def fetch_boxscore_hustle_v2(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """Deflections, contested shots and charges drawn."""
    endpoint = BOXSCORE_HUSTLE_V2
    check = _v3_box_check(endpoint, "boxScoreHustle", game_id)
    return _fetch_game_endpoint(session, cache, endpoint, game_id, {}, check, force=force)


def fetch_boxscore_matchups_v3(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """Who guarded whom, and for how long."""
    endpoint = BOXSCORE_MATCHUPS_V3
    check = _v3_box_check(endpoint, "boxScoreMatchups", game_id)
    return _fetch_game_endpoint(session, cache, endpoint, game_id, {}, check, force=force)


def fetch_boxscore_usage_v3(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """Each player's share of their team's possessions and box events."""
    endpoint = BOXSCORE_USAGE_V3
    check = _v3_box_check(endpoint, "boxScoreUsage", game_id)
    return _fetch_game_endpoint(
        session, cache, endpoint, game_id, WHOLE_GAME_BOX, check, force=force
    )


def fetch_playbyplay_v3(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """Our own play-by-play, with shot clock and descriptions."""

    def check(data: Payload) -> None:
        if not data.get("game", {}).get("actions"):
            raise NbaHttpError(f"{PLAYBYPLAY_V3} has no data for game {game_id}")

    return _fetch_game_endpoint(
        session, cache, PLAYBYPLAY_V3, game_id, WHOLE_GAME, check, force=force
    )


def fetch_playbyplay_v2(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> Payload:
    """The play-by-play in the v2 result-set shape that pbpstats parses.

    As of 2026-10-21 stats.nba.com answers `{}` for every game tried, 2024-25 and 2025-26 alike,
    whatever the parameters, so this raises `NbaHttpError` until the endpoint returns.
    """
    endpoint = PLAYBYPLAY_V2
    check = _v2_check(endpoint, game_id)
    return _fetch_game_endpoint(session, cache, endpoint, game_id, WHOLE_GAME, check, force=force)


class GameFetch(Protocol):
    def __call__(
        self, session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
    ) -> Payload: ...


# Every per-game endpoint, in the order `fetch_game` calls them.
GAME_ENDPOINTS: dict[str, GameFetch] = {
    BOXSCORE_TRADITIONAL_V3: fetch_boxscore_traditional_v3,
    BOXSCORE_TRADITIONAL_V2: fetch_boxscore_traditional_v2,
    BOXSCORE_ADVANCED_V3: fetch_boxscore_advanced_v3,
    BOXSCORE_HUSTLE_V2: fetch_boxscore_hustle_v2,
    BOXSCORE_MATCHUPS_V3: fetch_boxscore_matchups_v3,
    BOXSCORE_USAGE_V3: fetch_boxscore_usage_v3,
    PLAYBYPLAY_V3: fetch_playbyplay_v3,
    PLAYBYPLAY_V2: fetch_playbyplay_v2,
}


class FetchStatus(StrEnum):
    CACHED = "cached"
    FETCHED = "fetched"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EndpointResult:
    endpoint: str
    status: FetchStatus
    # Why a FAILED endpoint failed; None otherwise.
    error: str | None = None


def fetch_game(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> list[EndpointResult]:
    """Fetch every per-game endpoint for `game_id` into the cache, in `GAME_ENDPOINTS` order.

    An endpoint that answers badly (`NbaHttpError`) is reported as FAILED and the rest still run,
    so one endpoint's outage costs only its own data; a connection error, which `NbaSession` has
    already retried, propagates.
    """
    results: list[EndpointResult] = []
    for endpoint, fetch in GAME_ENDPOINTS.items():
        cached = not force and cache.exists(SOURCE, endpoint, game_id)
        try:
            fetch(session, cache, game_id, force=force)
        except NbaHttpError as exc:
            results.append(EndpointResult(endpoint, FetchStatus.FAILED, str(exc)))
        else:
            status = FetchStatus.CACHED if cached else FetchStatus.FETCHED
            results.append(EndpointResult(endpoint, status))
    return results

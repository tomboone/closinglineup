"""cdn.nba.com: the season schedule, today's scoreboard, and the live box score and play-by-play
(spec §4.2).

The CDN sits behind the same Akamai TLS fingerprinting as stats.nba.com, so it is fetched through
the same `NbaSession` (ADR-0002). Every function goes through `fetch_json`, so a response is in the
raw cache, under `raw/cdn/<endpoint>/<key>.json`, before anything parses it (spec §5). Each feed
declares the top-level keys its data is under, and a response without one raises `SchemaDrift`
(`sources.drift`).
"""

from datetime import UTC
from typing import Any

from closinglineup.sources.cache import RawCache
from closinglineup.sources.drift import Shape, top_level
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, fetch_json

SOURCE = "cdn"
CDN_BASE = "https://cdn.nba.com/static/json"

SCHEDULE = "scheduleleaguev2"
SCOREBOARD = "scoreboard"
BOXSCORE = "boxscore"
PLAYBYPLAY = "playbyplay"

SCHEDULE_URL = f"{CDN_BASE}/staticData/scheduleLeagueV2_1.json"
SCOREBOARD_URL = f"{CDN_BASE}/liveData/scoreboard/todaysScoreboard_00.json"

# A scoreboard snapshot's key: the instant it was fetched, in UTC, e.g. `20261021T233000Z`.
SCOREBOARD_KEY_FORMAT = "%Y%m%dT%H%M%SZ"
# The live data's `gameStatus`: 1 scheduled, 2 in progress, 3 final.
GAME_FINAL = 3

# Each feed's top-level keys, as recorded under `tests/fixtures/cdn/`.
SHAPES: dict[str, Shape] = {
    SCHEDULE: top_level("leagueSchedule"),
    SCOREBOARD: top_level("scoreboard"),
    BOXSCORE: top_level("game"),
    PLAYBYPLAY: top_level("game"),
}


def fetch_schedule(
    session: NbaSession, cache: RawCache, season: str, *, force: bool = False
) -> dict[str, Any]:
    """The schedule of `season` (e.g. `"2026-27"`), cached under that label.

    The CDN serves only the current season's file, and until the next season's is published that
    is still the last one, so a file for any other season raises `NbaHttpError` instead of being
    cached under the wrong label. The schedule fills in as the season goes (postponements, Cup
    knockout and postseason matchups), so a refresh passes `force`.
    """

    def check(data: dict[str, Any]) -> None:
        found = data.get("leagueSchedule", {}).get("seasonYear")
        if found != season:
            raise NbaHttpError(f"{SCHEDULE_URL} holds season {found}, not {season}")

    return fetch_json(
        session,
        cache,
        SOURCE,
        SCHEDULE,
        season,
        SCHEDULE_URL,
        force=force,
        check=check,
        shape=SHAPES[SCHEDULE],
    )


def fetch_todays_scoreboard(session: NbaSession, cache: RawCache) -> dict[str, Any]:
    """Today's scoreboard, cached as a snapshot keyed by the instant it was fetched.

    It changes all evening as games start and go final, so every poll is fetched and kept, rather
    than one write-once file per date.
    """
    key = cache.clock.now().astimezone(UTC).strftime(SCOREBOARD_KEY_FORMAT)
    return fetch_json(
        session, cache, SOURCE, SCOREBOARD, key, SCOREBOARD_URL, shape=SHAPES[SCOREBOARD]
    )


def fetch_live_boxscore(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> dict[str, Any]:
    """The live box score of a final game, cached under its game ID.

    The cache is write-once, so a box of a game that is not yet final raises `NbaHttpError`
    rather than standing in for the game's box forever.
    """

    def check(data: dict[str, Any]) -> None:
        status = data.get("game", {}).get("gameStatus")
        if status != GAME_FINAL:
            raise NbaHttpError(f"game {game_id} is not final (gameStatus {status})")

    url = f"{CDN_BASE}/liveData/{BOXSCORE}/{BOXSCORE}_{game_id}.json"
    shape = SHAPES[BOXSCORE]
    return fetch_json(
        session, cache, SOURCE, BOXSCORE, game_id, url, force=force, check=check, shape=shape
    )


def fetch_live_pbp(
    session: NbaSession, cache: RawCache, game_id: str, *, force: bool = False
) -> dict[str, Any]:
    """The live play-by-play of a final game, cached under its game ID.

    The feed carries no game status, so a game is final once it has its `game`/`end` action;
    without one it raises `NbaHttpError`, as `fetch_live_boxscore` does.
    """

    def check(data: dict[str, Any]) -> None:
        actions: list[dict[str, Any]] = data.get("game", {}).get("actions", [])
        if not any(a.get("actionType") == "game" and a.get("subType") == "end" for a in actions):
            raise NbaHttpError(f"game {game_id} is not final (no game end action)")

    url = f"{CDN_BASE}/liveData/{PLAYBYPLAY}/{PLAYBYPLAY}_{game_id}.json"
    shape = SHAPES[PLAYBYPLAY]
    return fetch_json(
        session, cache, SOURCE, PLAYBYPLAY, game_id, url, force=force, check=check, shape=shape
    )

"""stats.nba.com endpoint functions, one per endpoint (spec §4.1).

Parameter names follow nba_api's documentation for each endpoint, and every parameter nba_api
fills with a default is passed explicitly, empty strings included; the defaults are nba_api
1.11.4's. Every function goes through `fetch_json`, so a response is in the raw cache, under
`raw/nba_stats/<endpoint>/<key>.json`, before anything parses it (spec §5).

stats.nba.com answers a request with nothing in it, such as a game not yet played, an endpoint it
no longer serves or a season with no games yet, with a 200 and an empty shell rather than an
error. Each function checks for that shell and raises `NbaHttpError`, since the cache is
write-once and would otherwise keep it in place of the data forever. A game in progress is not
detected: only final games are fetched (spec §3.3).

The season-wide endpoints are keyed by season, and season type where they take one, so they hold
whatever the season looked like when first fetched; a refresh during the season passes `force`.

Each function declares the shape it expects of its response (`sources.drift`): its result sets'
names and columns for a v2 response, its top-level keys for a v3 one. A response missing any of
them is not cached and raises `SchemaDrift`, which the batch fetches report as DRIFT.
"""

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

from closinglineup.season import NBA_LEAGUE_ID
from closinglineup.sources.cache import RawCache
from closinglineup.sources.drift import SchemaDrift, Shape, result_sets, top_level
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, fetch_json
from closinglineup.teams import TEAMS

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
SCOREBOARD_V3 = "scoreboardv3"
SCHEDULE_LEAGUE_V2 = "scheduleleaguev2"
COMMON_TEAM_ROSTER = "commonteamroster"
COMMON_ALL_PLAYERS = "commonallplayers"
PLAYER_GAME_LOGS = "playergamelogs"
SHOT_CHART_DETAIL = "shotchartdetail"
LEAGUE_DASH_LINEUPS = "leaguedashlineups"
LEAGUE_PLAYER_ON_DETAILS = "leagueplayerondetails"
LEAGUE_DASH_PLAYER_STATS = "leaguedashplayerstats"
LEAGUE_DASH_TEAM_STATS = "leaguedashteamstats"

# Periods 0 to 14 cover any game: 0 means "from the start", and 14 is past the most overtimes
# ever played. A `RangeType` of 0 ignores `StartRange` and `EndRange`.
WHOLE_GAME = {"StartPeriod": "0", "EndPeriod": "14"}
WHOLE_GAME_BOX = {**WHOLE_GAME, "StartRange": "0", "EndRange": "0", "RangeType": "0"}
# `boxscoretraditionalv2` returns no rows for `RangeType` 0, so it takes the elapsed-time range
# pbpstats's own loader sends: 0 to 55800 tenths of a second, which covers nine overtimes.
# `EndRange` 28800 stops at the end of regulation. Probed on 2026-10-21.
WHOLE_GAME_BOX_V2 = {**WHOLE_GAME, "StartRange": "0", "EndRange": "55800", "RangeType": "2"}

# Every filter the league dashboards share, at nba_api's "no filter" default: `Totals` over the
# whole season, unadjusted and unranked.
DASH_FILTERS = {
    "LastNGames": "0",
    "Month": "0",
    "OpponentTeamID": "0",
    "PaceAdjust": "N",
    "PerMode": "Totals",
    "Period": "0",
    "PlusMinus": "N",
    "Rank": "N",
    "DateFrom": "",
    "DateTo": "",
    "GameSegment": "",
    "LeagueID": NBA_LEAGUE_ID,
    "Location": "",
    "Outcome": "",
    "SeasonSegment": "",
    "VsConference": "",
    "VsDivision": "",
}
# The further filters `leaguedashlineups`, `leaguedashplayerstats` and `leaguedashteamstats` take.
LEAGUE_DASH_FILTERS = {
    **DASH_FILTERS,
    "Conference": "",
    "Division": "",
    "PORound": "",
    "ShotClockRange": "",
    "TeamID": "",
}
# And the further ones both stats dashboards take.
STATS_DASH_FILTERS = {
    **LEAGUE_DASH_FILTERS,
    "GameScope": "",
    "PlayerExperience": "",
    "PlayerPosition": "",
    "StarterBench": "",
    "TwoWay": "",
}
# `scoreboardv3`'s `gameStatus`: 1 scheduled, 2 in progress, 3 final.
GAME_FINAL = 3

# The shape of each endpoint's response, as recorded under `tests/fixtures/nba_stats/`. The stats
# dashboards' shapes vary with the measure type, so they are in `DASH_SHAPES` instead.
# `playbyplayv2` answers `{}` for every game, so it has no shape on record and none is checked.
SHAPES: dict[str, Shape] = {
    BOXSCORE_TRADITIONAL_V3: top_level("boxScoreTraditional"),
    BOXSCORE_TRADITIONAL_V2: result_sets(
        PlayerStats=(
            "GAME_ID TEAM_ID TEAM_ABBREVIATION TEAM_CITY PLAYER_ID PLAYER_NAME NICKNAME "
            "START_POSITION COMMENT MIN FGM FGA FG_PCT FG3M FG3A FG3_PCT FTM FTA FT_PCT OREB DREB "
            "REB AST STL BLK TO PF PTS PLUS_MINUS"
        ),
        TeamStats=(
            "GAME_ID TEAM_ID TEAM_NAME TEAM_ABBREVIATION TEAM_CITY MIN FGM FGA FG_PCT FG3M FG3A "
            "FG3_PCT FTM FTA FT_PCT OREB DREB REB AST STL BLK TO PF PTS PLUS_MINUS"
        ),
        TeamStarterBenchStats=(
            "GAME_ID TEAM_ID TEAM_NAME TEAM_ABBREVIATION TEAM_CITY STARTERS_BENCH MIN FGM FGA "
            "FG_PCT FG3M FG3A FG3_PCT FTM FTA FT_PCT OREB DREB REB AST STL BLK TO PF PTS"
        ),
    ),
    BOXSCORE_ADVANCED_V3: top_level("boxScoreAdvanced"),
    BOXSCORE_HUSTLE_V2: top_level("boxScoreHustle"),
    BOXSCORE_MATCHUPS_V3: top_level("boxScoreMatchups"),
    BOXSCORE_USAGE_V3: top_level("boxScoreUsage"),
    PLAYBYPLAY_V3: top_level("game"),
    SCOREBOARD_V3: top_level("scoreboard"),
    SCHEDULE_LEAGUE_V2: top_level("leagueSchedule"),
    COMMON_TEAM_ROSTER: result_sets(
        CommonTeamRoster=(
            "TeamID SEASON LeagueID PLAYER NICKNAME PLAYER_SLUG NUM POSITION HEIGHT WEIGHT "
            "BIRTH_DATE AGE EXP SCHOOL PLAYER_ID HOW_ACQUIRED"
        ),
        Coaches=(
            "TEAM_ID SEASON COACH_ID FIRST_NAME LAST_NAME COACH_NAME IS_ASSISTANT COACH_TYPE "
            "SORT_SEQUENCE SUB_SORT_SEQUENCE"
        ),
    ),
    COMMON_ALL_PLAYERS: result_sets(
        CommonAllPlayers=(
            "PERSON_ID DISPLAY_LAST_COMMA_FIRST DISPLAY_FIRST_LAST ROSTERSTATUS FROM_YEAR TO_YEAR "
            "PLAYERCODE PLAYER_SLUG TEAM_ID TEAM_CITY TEAM_NAME TEAM_ABBREVIATION TEAM_CODE "
            "TEAM_SLUG GAMES_PLAYED_FLAG OTHERLEAGUE_EXPERIENCE_CH"
        ),
    ),
    PLAYER_GAME_LOGS: result_sets(
        PlayerGameLogs=(
            "SEASON_YEAR PLAYER_ID PLAYER_NAME NICKNAME TEAM_ID TEAM_ABBREVIATION TEAM_NAME "
            "GAME_ID GAME_DATE MATCHUP WL MIN FGM FGA FG_PCT FG3M FG3A FG3_PCT FTM FTA FT_PCT "
            "OREB DREB REB AST TOV STL BLK BLKA PF PFD PTS PLUS_MINUS NBA_FANTASY_PTS DD2 TD3 "
            "WNBA_FANTASY_PTS FP_HIGH_SCORE GP_RANK W_RANK L_RANK W_PCT_RANK MIN_RANK FGM_RANK "
            "FGA_RANK FG_PCT_RANK FG3M_RANK FG3A_RANK FG3_PCT_RANK FTM_RANK FTA_RANK FT_PCT_RANK "
            "OREB_RANK DREB_RANK REB_RANK AST_RANK TOV_RANK STL_RANK BLK_RANK BLKA_RANK PF_RANK "
            "PFD_RANK PTS_RANK PLUS_MINUS_RANK NBA_FANTASY_PTS_RANK DD2_RANK TD3_RANK "
            "WNBA_FANTASY_PTS_RANK FP_HIGH_SCORE_RANK AVAILABLE_FLAG MIN_SEC TEAM_COUNT"
        ),
    ),
    SHOT_CHART_DETAIL: result_sets(
        Shot_Chart_Detail=(
            "GRID_TYPE GAME_ID GAME_EVENT_ID PLAYER_ID PLAYER_NAME TEAM_ID TEAM_NAME PERIOD "
            "MINUTES_REMAINING SECONDS_REMAINING EVENT_TYPE ACTION_TYPE SHOT_TYPE SHOT_ZONE_BASIC "
            "SHOT_ZONE_AREA SHOT_ZONE_RANGE SHOT_DISTANCE LOC_X LOC_Y SHOT_ATTEMPTED_FLAG "
            "SHOT_MADE_FLAG GAME_DATE HTM VTM"
        ),
        LeagueAverages="GRID_TYPE SHOT_ZONE_BASIC SHOT_ZONE_AREA SHOT_ZONE_RANGE FGA FGM FG_PCT",
    ),
    LEAGUE_DASH_LINEUPS: result_sets(
        Lineups=(
            "GROUP_SET GROUP_ID GROUP_NAME TEAM_ID TEAM_ABBREVIATION GP W L W_PCT MIN "
            "E_OFF_RATING OFF_RATING E_DEF_RATING DEF_RATING E_NET_RATING NET_RATING AST_PCT "
            "AST_TO AST_RATIO OREB_PCT DREB_PCT REB_PCT TM_TOV_PCT EFG_PCT TS_PCT E_PACE PACE "
            "PACE_PER40 POSS PIE GP_RANK W_RANK L_RANK W_PCT_RANK MIN_RANK OFF_RATING_RANK "
            "DEF_RATING_RANK NET_RATING_RANK AST_PCT_RANK AST_TO_RANK AST_RATIO_RANK "
            "OREB_PCT_RANK DREB_PCT_RANK REB_PCT_RANK TM_TOV_PCT_RANK EFG_PCT_RANK TS_PCT_RANK "
            "PACE_RANK PIE_RANK SUM_TIME_PLAYED"
        ),
    ),
    LEAGUE_PLAYER_ON_DETAILS: result_sets(
        PlayersOnCourtLeaguePlayerDetails=(
            "GROUP_SET TEAM_ID TEAM_ABBREVIATION TEAM_NAME VS_PLAYER_ID VS_PLAYER_NAME "
            "COURT_STATUS GP W L W_PCT MIN E_OFF_RATING OFF_RATING E_DEF_RATING DEF_RATING "
            "E_NET_RATING NET_RATING AST_PCT AST_TO AST_RATIO OREB_PCT DREB_PCT REB_PCT "
            "TM_TOV_PCT EFG_PCT TS_PCT E_PACE PACE PACE_PER40 POSS PIE GP_RANK W_RANK L_RANK "
            "W_PCT_RANK MIN_RANK OFF_RATING_RANK DEF_RATING_RANK NET_RATING_RANK AST_PCT_RANK "
            "AST_TO_RANK AST_RATIO_RANK OREB_PCT_RANK DREB_PCT_RANK REB_PCT_RANK TM_TOV_PCT_RANK "
            "EFG_PCT_RANK TS_PCT_RANK PACE_RANK PIE_RANK"
        ),
    ),
}

type Payload = dict[str, Any]
type Check = Callable[[Payload], None]


class SeasonType(StrEnum):
    """The `SeasonType` values a season-wide endpoint is fetched for."""

    REGULAR_SEASON = "Regular Season"
    PLAYOFFS = "Playoffs"
    PLAY_IN = "PlayIn"

    @property
    def slug(self) -> str:
        """The season type as it appears in a cache key, e.g. `regular-season`."""
        return self.value.lower().replace(" ", "-")


class MeasureType(StrEnum):
    """The `MeasureType` values the league stats dashboards are fetched for."""

    BASE = "Base"
    ADVANCED = "Advanced"


# The stats dashboards' shapes, by measure type: only `Advanced` is on record.
DASH_SHAPES: dict[tuple[str, MeasureType], Shape] = {
    (LEAGUE_DASH_PLAYER_STATS, MeasureType.ADVANCED): result_sets(
        LeagueDashPlayerStats=(
            "PLAYER_ID PLAYER_NAME NICKNAME TEAM_ID TEAM_ABBREVIATION AGE GP W L W_PCT MIN "
            "E_OFF_RATING OFF_RATING sp_work_OFF_RATING E_DEF_RATING DEF_RATING "
            "sp_work_DEF_RATING E_NET_RATING NET_RATING sp_work_NET_RATING AST_PCT AST_TO "
            "AST_RATIO OREB_PCT DREB_PCT REB_PCT TM_TOV_PCT E_TOV_PCT EFG_PCT TS_PCT USG_PCT "
            "E_USG_PCT E_PACE PACE PACE_PER40 sp_work_PACE PIE POSS FGM FGA FGM_PG FGA_PG "
            "FG_PCT GP_RANK W_RANK L_RANK W_PCT_RANK MIN_RANK E_OFF_RATING_RANK "
            "OFF_RATING_RANK sp_work_OFF_RATING_RANK E_DEF_RATING_RANK DEF_RATING_RANK "
            "sp_work_DEF_RATING_RANK E_NET_RATING_RANK NET_RATING_RANK "
            "sp_work_NET_RATING_RANK AST_PCT_RANK AST_TO_RANK AST_RATIO_RANK OREB_PCT_RANK "
            "DREB_PCT_RANK REB_PCT_RANK TM_TOV_PCT_RANK E_TOV_PCT_RANK EFG_PCT_RANK "
            "TS_PCT_RANK USG_PCT_RANK E_USG_PCT_RANK E_PACE_RANK PACE_RANK sp_work_PACE_RANK "
            "PIE_RANK FGM_RANK FGA_RANK FGM_PG_RANK FGA_PG_RANK FG_PCT_RANK TEAM_COUNT"
        ),
    ),
    (LEAGUE_DASH_TEAM_STATS, MeasureType.ADVANCED): result_sets(
        LeagueDashTeamStats=(
            "TEAM_ID TEAM_NAME GP W L W_PCT MIN E_OFF_RATING OFF_RATING E_DEF_RATING "
            "DEF_RATING E_NET_RATING NET_RATING AST_PCT AST_TO AST_RATIO OREB_PCT DREB_PCT "
            "REB_PCT TM_TOV_PCT EFG_PCT TS_PCT E_PACE PACE PACE_PER40 POSS PIE GP_RANK W_RANK "
            "L_RANK W_PCT_RANK MIN_RANK OFF_RATING_RANK DEF_RATING_RANK NET_RATING_RANK "
            "AST_PCT_RANK AST_TO_RANK AST_RATIO_RANK OREB_PCT_RANK DREB_PCT_RANK REB_PCT_RANK "
            "TM_TOV_PCT_RANK EFG_PCT_RANK TS_PCT_RANK PACE_RANK PIE_RANK"
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class Request:
    """One stats.nba.com request: where it is cached, what it sends, and what counts as data."""

    endpoint: str
    key: str
    params: dict[str, str]
    check: Check
    # What the response must look like; None checks nothing (`sources.drift`).
    shape: Shape | None = None


def _fetch(session: NbaSession, cache: RawCache, request: Request, *, force: bool) -> Payload:
    url = f"{STATS_BASE}/{request.endpoint}"
    return fetch_json(
        session,
        cache,
        SOURCE,
        request.endpoint,
        request.key,
        url,
        request.params,
        force=force,
        check=request.check,
        shape=request.shape,
    )


def _v3_box_check(endpoint: str, root: str, game_id: str) -> Check:
    """A v3 box score of a game with no data has team ID 0 in place of its home team."""

    def check(data: Payload) -> None:
        if not data.get(root, {}).get("homeTeam", {}).get("teamId"):
            raise NbaHttpError(f"{endpoint} has no data for game {game_id}")

    return check


def _v2_check(endpoint: str, subject: str) -> Check:
    """A v2 response with no data has an empty first result set, or no result sets.

    `subject` names what has no data in the error, e.g. `game 0022500006`.
    """

    def check(data: Payload) -> None:
        result_sets: list[Payload] = data.get("resultSets") or [{}]
        if not result_sets[0].get("rowSet"):
            raise NbaHttpError(f"{endpoint} has no data for {subject}")

    return check


def _v2_request(endpoint: str, key: str, params: dict[str, str]) -> Request:
    """A v2 request, which has no data with no rows, of the shape `SHAPES` declares for it."""
    return Request(endpoint, key, params, _v2_check(endpoint, key), SHAPES.get(endpoint))


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
    params = {"GameID": game_id, **params}
    request = Request(endpoint, game_id, params, check, SHAPES.get(endpoint))
    return _fetch(session, cache, request, force=force)


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
    check = _v2_check(endpoint, f"game {game_id}")
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
    check = _v2_check(endpoint, f"game {game_id}")
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
    # Fetched, but not of the shape its endpoint declares, so not cached (`SchemaDrift`).
    DRIFT = "drift"


@dataclass(frozen=True, slots=True)
class EndpointResult:
    endpoint: str
    # The cache key fetched, e.g. the game ID.
    key: str
    status: FetchStatus
    # Why a FAILED endpoint failed, or how a DRIFT one differs; None otherwise.
    error: str | None = None
    source: str = SOURCE


def _attempt(
    cache: RawCache, endpoint: str, key: str, fetch: Callable[[], object], *, force: bool
) -> EndpointResult:
    """Run `fetch` for one cache key and report it, turning `NbaHttpError` into FAILED and
    `SchemaDrift` into DRIFT."""
    cached = not force and cache.exists(SOURCE, endpoint, key)
    try:
        fetch()
    except NbaHttpError as exc:
        return EndpointResult(endpoint, key, FetchStatus.FAILED, str(exc))
    except SchemaDrift as exc:
        return EndpointResult(endpoint, key, FetchStatus.DRIFT, str(exc))
    return EndpointResult(endpoint, key, FetchStatus.CACHED if cached else FetchStatus.FETCHED)


def fetch_game(
    session: NbaSession,
    cache: RawCache,
    game_id: str,
    *,
    force: bool = False,
    skip: Collection[str] = (),
) -> list[EndpointResult]:
    """Fetch every per-game endpoint for `game_id` but `skip` into the cache, in `GAME_ENDPOINTS`
    order.

    An endpoint that answers badly (`NbaHttpError`) is reported as FAILED and the rest still run,
    so one endpoint's outage costs only its own data, as is one whose response has drifted
    (`SchemaDrift`), as DRIFT; `NbaUnreachableError`, raised once
    `NbaSession` has retried a connection error or timeout, propagates.
    """
    return [
        _attempt(
            cache,
            endpoint,
            game_id,
            lambda fetch=fetch: fetch(session, cache, game_id, force=force),
            force=force,
        )
        for endpoint, fetch in GAME_ENDPOINTS.items()
        if endpoint not in skip
    ]


def _key(*parts: str) -> str:
    """A season-wide cache key: its parts joined by `_`, e.g. `1610612744_2026-27` (spec §5)."""
    return "_".join(parts)


def _scoreboard_v3(game_date: date) -> Request:
    day = game_date.isoformat()

    def check(data: Payload) -> None:
        board: Payload = data.get("scoreboard") or {}
        if board.get("gameDate") != day:
            raise NbaHttpError(f"{SCOREBOARD_V3} has no data for {day}")
        games: list[Payload] = board.get("games") or []
        if not_final := [g.get("gameId") for g in games if g.get("gameStatus") != GAME_FINAL]:
            raise NbaHttpError(f"{SCOREBOARD_V3} for {day} has games not final: {not_final}")

    params = {"GameDate": day, "LeagueID": NBA_LEAGUE_ID}
    return Request(SCOREBOARD_V3, day, params, check, SHAPES[SCOREBOARD_V3])


def _schedule_league_v2(season: str) -> Request:
    def check(data: Payload) -> None:
        schedule: Payload = data.get("leagueSchedule") or {}
        if schedule.get("seasonYear") != season or not schedule.get("gameDates"):
            raise NbaHttpError(f"{SCHEDULE_LEAGUE_V2} has no data for {season}")

    params = {"LeagueID": NBA_LEAGUE_ID, "Season": season}
    return Request(SCHEDULE_LEAGUE_V2, season, params, check, SHAPES[SCHEDULE_LEAGUE_V2])


def _common_team_roster(team_id: int, season: str) -> Request:
    key = _key(str(team_id), season)
    params = {"TeamID": str(team_id), "Season": season, "LeagueID": NBA_LEAGUE_ID}
    return _v2_request(COMMON_TEAM_ROSTER, key, params)


def _common_all_players(season: str) -> Request:
    # `IsOnlyCurrentSeason=0` lists every player NBA.com knows, not only this season's, so a
    # player who has not yet appeared still resolves.
    params = {"IsOnlyCurrentSeason": "0", "LeagueID": NBA_LEAGUE_ID, "Season": season}
    return _v2_request(COMMON_ALL_PLAYERS, season, params)


def _player_game_logs(season: str, season_type: SeasonType) -> Request:
    key = _key(season, season_type.slug)
    # nba_api leaves `MeasureType` and `OpponentTeamID` unset, so they are not sent at all. It
    # defaults `LeagueID` to 00 because an empty one adds NBA Cup rows (87 games for 2023-24).
    params = {
        "DateFrom": "",
        "DateTo": "",
        "GameSegment": "",
        "LastNGames": "",
        "LeagueID": NBA_LEAGUE_ID,
        "Location": "",
        "Month": "",
        "Outcome": "",
        "PORound": "",
        "PerMode": "",
        "Period": "",
        "PlayerID": "",
        "Season": season,
        "SeasonSegment": "",
        "SeasonType": season_type,
        "ShotClockRange": "",
        "TeamID": "",
        "VsConference": "",
        "VsDivision": "",
    }
    return _v2_request(PLAYER_GAME_LOGS, key, params)


def _shot_chart_detail(player_id: int, season: str, season_type: SeasonType) -> Request:
    key = _key(str(player_id), season, season_type.slug)
    # `TeamID=0` is every team the player shot for; `ContextMeasure=FGA` is every attempt, where
    # nba_api's default, `PTS`, is only the makes.
    params = {
        "TeamID": "0",
        "PlayerID": str(player_id),
        "ContextMeasure": "FGA",
        "LastNGames": "0",
        "LeagueID": NBA_LEAGUE_ID,
        "Month": "0",
        "OpponentTeamID": "0",
        "Period": "0",
        "SeasonType": season_type,
        "AheadBehind": "",
        "ClutchTime": "",
        "ContextFilter": "",
        "DateFrom": "",
        "DateTo": "",
        "EndPeriod": "",
        "EndRange": "",
        "GameID": "",
        "GameSegment": "",
        "Location": "",
        "Outcome": "",
        "PlayerPosition": "",
        "PointDiff": "",
        "Position": "",
        "RangeType": "",
        "RookieYear": "",
        "Season": season,
        "SeasonSegment": "",
        "StartPeriod": "",
        "StartRange": "",
        "VsConference": "",
        "VsDivision": "",
    }
    return _v2_request(SHOT_CHART_DETAIL, key, params)


def _league_dash_lineups(season: str, season_type: SeasonType, last_n_games: int) -> Request:
    # A whole-season pull is keyed by season and season type alone; a last-n pull adds `last<n>`.
    parts = [season, season_type.slug] + ([f"last{last_n_games}"] if last_n_games else [])
    key = _key(*parts)
    params = {
        **LEAGUE_DASH_FILTERS,
        "GroupQuantity": "5",
        "LastNGames": str(last_n_games),
        "MeasureType": MeasureType.ADVANCED,
        "Season": season,
        "SeasonType": season_type,
    }
    return _v2_request(LEAGUE_DASH_LINEUPS, key, params)


def _league_player_on_details(team_id: int, season: str, season_type: SeasonType) -> Request:
    key = _key(str(team_id), season, season_type.slug)
    params = {
        **DASH_FILTERS,
        "TeamID": str(team_id),
        "MeasureType": MeasureType.ADVANCED,
        "Season": season,
        "SeasonType": season_type,
    }
    return _v2_request(LEAGUE_PLAYER_ON_DETAILS, key, params)


def _league_dash_player_stats(
    season: str, season_type: SeasonType, measure_type: MeasureType
) -> Request:
    key = _key(season, season_type.slug, measure_type.lower())
    params = {
        **STATS_DASH_FILTERS,
        "College": "",
        "Country": "",
        "DraftPick": "",
        "DraftYear": "",
        "Height": "",
        "Weight": "",
        "MeasureType": measure_type,
        "Season": season,
        "SeasonType": season_type,
    }
    shape = DASH_SHAPES.get((LEAGUE_DASH_PLAYER_STATS, measure_type))
    return Request(
        LEAGUE_DASH_PLAYER_STATS, key, params, _v2_check(LEAGUE_DASH_PLAYER_STATS, key), shape
    )


def _league_dash_team_stats(
    season: str, season_type: SeasonType, measure_type: MeasureType
) -> Request:
    key = _key(season, season_type.slug, measure_type.lower())
    params = {
        **STATS_DASH_FILTERS,
        "MeasureType": measure_type,
        "Season": season,
        "SeasonType": season_type,
    }
    shape = DASH_SHAPES.get((LEAGUE_DASH_TEAM_STATS, measure_type))
    return Request(
        LEAGUE_DASH_TEAM_STATS, key, params, _v2_check(LEAGUE_DASH_TEAM_STATS, key), shape
    )


def fetch_scoreboard_v3(
    session: NbaSession, cache: RawCache, game_date: date, *, force: bool = False
) -> Payload:
    """Each game's status on an Eastern-time date, cached under the ISO date.

    A board with a game not yet final raises `NbaHttpError`, as a box score of one does, since
    its statuses would otherwise be kept as the date's for good; a date with no games is cached.
    A postponed game leaves its original date's board once it is rescheduled (probed 2026-10-21:
    CHA at LAL, postponed from 2025-01-09, is not on that board), so a date holding one fails
    only until then.
    """
    return _fetch(session, cache, _scoreboard_v3(game_date), force=force)


def fetch_schedule_league_v2(
    session: NbaSession, cache: RawCache, season: str, *, force: bool = False
) -> Payload:
    """The schedule of any season, in cdn.nba.com's `scheduleLeagueV2` shape, cached under the
    season.

    cdn.nba.com serves only the current season's (`nba_cdn.fetch_schedule`), so a past season's
    comes from here. It fills in as the season goes, as that one does, so a refresh passes
    `force`. Probed 2026-10-22: 2025-26 lists 1,400 games, all final, and 2026-27 the season so
    far, updated that morning.
    """
    return _fetch(session, cache, _schedule_league_v2(season), force=force)


def fetch_common_team_roster(
    session: NbaSession, cache: RawCache, team_id: int, season: str, *, force: bool = False
) -> Payload:
    """A team's roster and coaches for a season, cached under `<team_id>_<season>`."""
    return _fetch(session, cache, _common_team_roster(team_id, season), force=force)


def fetch_common_all_players(
    session: NbaSession, cache: RawCache, season: str, *, force: bool = False
) -> Payload:
    """Every player NBA.com knows, with their team as of `season`, cached under the season."""
    return _fetch(session, cache, _common_all_players(season), force=force)


def fetch_player_game_logs(
    session: NbaSession,
    cache: RawCache,
    season: str,
    season_type: SeasonType,
    *,
    force: bool = False,
) -> Payload:
    """Every player's line in every game of a season type, for the backfill cross-check.

    Cached under `<season>_<season type>`, e.g. `2026-27_regular-season`.
    """
    return _fetch(session, cache, _player_game_logs(season, season_type), force=force)


def fetch_shot_chart_detail(
    session: NbaSession,
    cache: RawCache,
    player_id: int,
    season: str,
    season_type: SeasonType,
    *,
    force: bool = False,
) -> Payload:
    """Every shot a player attempted in a season type, for every team.

    Cached under `<player_id>_<season>_<season type>`: spec §5's `<player_id>_<season>` with the
    season type added, so a player's playoff shots do not land on their regular-season file.
    """
    return _fetch(session, cache, _shot_chart_detail(player_id, season, season_type), force=force)


def fetch_league_dash_lineups(
    session: NbaSession,
    cache: RawCache,
    season: str,
    season_type: SeasonType,
    last_n_games: int = 0,
    *,
    force: bool = False,
) -> Payload:
    """Every 5-man lineup's advanced totals, which reconciliation checks our net ratings against.

    `last_n_games` of 0 is the whole season. Cached under `<season>_<season type>`, plus
    `_last<n>` for a last-n pull.
    """
    request = _league_dash_lineups(season, season_type, last_n_games)
    return _fetch(session, cache, request, force=force)


def fetch_league_player_on_details(
    session: NbaSession,
    cache: RawCache,
    team_id: int,
    season: str,
    season_type: SeasonType,
    *,
    force: bool = False,
) -> Payload:
    """A team's advanced ratings with each of its players on the court, which reconciliation
    checks our on/off against. Cached under `<team_id>_<season>_<season type>`."""
    request = _league_player_on_details(team_id, season, season_type)
    return _fetch(session, cache, request, force=force)


def league_dash_lineups_key(season: str, season_type: SeasonType) -> str:
    """The cache key of a whole-season `leaguedashlineups` pull."""
    return _league_dash_lineups(season, season_type, 0).key


def league_player_on_details_key(team_id: int, season: str, season_type: SeasonType) -> str:
    """The cache key of a team's `leagueplayerondetails` pull."""
    return _league_player_on_details(team_id, season, season_type).key


def fetch_league_dash_player_stats(
    session: NbaSession,
    cache: RawCache,
    season: str,
    season_type: SeasonType,
    measure_type: MeasureType,
    *,
    force: bool = False,
) -> Payload:
    """Every player's season totals, for cross-checks.

    Cached under `<season>_<season type>_<measure type>`, e.g. `2026-27_regular-season_base`.
    """
    request = _league_dash_player_stats(season, season_type, measure_type)
    return _fetch(session, cache, request, force=force)


def fetch_league_dash_team_stats(
    session: NbaSession,
    cache: RawCache,
    season: str,
    season_type: SeasonType,
    measure_type: MeasureType,
    *,
    force: bool = False,
) -> Payload:
    """Every team's season totals, for cross-checks, keyed as `fetch_league_dash_player_stats`."""
    request = _league_dash_team_stats(season, season_type, measure_type)
    return _fetch(session, cache, request, force=force)


def _fetch_each(
    session: NbaSession, cache: RawCache, requests: Iterable[Request], *, force: bool
) -> list[EndpointResult]:
    """Fetch each request in turn, carrying on past one that fails, as `fetch_game` does."""
    return [
        _attempt(
            cache,
            request.endpoint,
            request.key,
            lambda request=request: _fetch(session, cache, request, force=force),
            force=force,
        )
        for request in requests
    ]


def fetch_rosters(
    session: NbaSession, cache: RawCache, season: str, *, force: bool = False
) -> list[EndpointResult]:
    """The daily roster pull (spec §3.3): every team's roster, in `TEAMS` order, then every
    player."""
    requests = [_common_team_roster(team.nba_team_id, season) for team in TEAMS]
    requests.append(_common_all_players(season))
    return _fetch_each(session, cache, requests, force=force)


def fetch_player_shots(
    session: NbaSession,
    cache: RawCache,
    player_id: int,
    season: str,
    season_type: SeasonType,
    *,
    force: bool = False,
) -> list[EndpointResult]:
    """One player's shot chart, reported as the batch fetches are."""
    request = _shot_chart_detail(player_id, season, season_type)
    return _fetch_each(session, cache, [request], force=force)


def fetch_aggregates(
    session: NbaSession,
    cache: RawCache,
    season: str,
    season_type: SeasonType,
    *,
    force: bool = False,
) -> list[EndpointResult]:
    """The league-wide pulls reconciliation and the cross-checks compare against (spec §3.3,
    §11): game logs, whole-season lineups, both stats dashboards in each measure type, and every
    team's on/off, in `TEAMS` order."""
    requests = [
        _player_game_logs(season, season_type),
        _league_dash_lineups(season, season_type, 0),
        *(_league_dash_player_stats(season, season_type, m) for m in MeasureType),
        *(_league_dash_team_stats(season, season_type, m) for m in MeasureType),
        *(_league_player_on_details(t.nba_team_id, season, season_type) for t in TEAMS),
    ]
    return _fetch_each(session, cache, requests, force=force)


def fetch_reconciliation(
    session: NbaSession,
    cache: RawCache,
    season: str,
    season_type: SeasonType,
    team_ids: Iterable[int],
    *,
    force: bool = False,
) -> list[EndpointResult]:
    """What reconciling `team_ids` reads: the whole-season lineups, then each team's on/off."""
    requests = [
        _league_dash_lineups(season, season_type, 0),
        *(_league_player_on_details(team_id, season, season_type) for team_id in team_ids),
    ]
    return _fetch_each(session, cache, requests, force=force)

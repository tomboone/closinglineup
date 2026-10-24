import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from curl_cffi import requests

from closinglineup.clock import FakeClock
from closinglineup.sources import nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, RateLimiter
from closinglineup.sources.nba_stats import EndpointResult, FetchStatus, MeasureType, SeasonType
from closinglineup.teams import TEAMS
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
    with NbaSession(requests.Session[requests.Response], RateLimiter(1e6, clock), clock) as s:
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


def _v2_shell(endpoint: str) -> dict[str, Any]:
    """A v2 response as stats.nba.com returns it with no rows: the endpoint's recorded result
    sets, headers and all, each with no rows."""
    (path,) = (FIXTURES / endpoint).glob("*.json")
    payload: dict[str, Any] = json.loads(path.read_bytes())
    for result_set in payload["resultSets"]:
        result_set["rowSet"] = []
    return payload


EMPTY_SHELLS: dict[str, bytes] = {
    "boxscoretraditionalv3": json.dumps(_v3_box_shell("boxScoreTraditional")).encode(),
    "boxscoretraditionalv2": json.dumps(_v2_shell("boxscoretraditionalv2")).encode(),
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

    assert results == [EndpointResult(e, GAME_ID, FetchStatus.FETCHED) for e in WITH_DATA] + [
        EndpointResult(
            "playbyplayv2",
            GAME_ID,
            FetchStatus.FAILED,
            f"playbyplayv2 has no data for game {GAME_ID}",
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
    assert results == [EndpointResult(e, GAME_ID, FetchStatus.CACHED) for e in ENDPOINTS]


def test_fetch_game_leaves_out_the_skipped_endpoints(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    get = _script(session, monkeypatch, *map(fixture_body, WITH_DATA))

    results = nba_stats.fetch_game(session, cache, GAME_ID, skip={"playbyplayv2"})

    assert [url.rsplit("/", 1)[1] for url, _ in get.calls] == WITH_DATA
    assert results == [EndpointResult(e, GAME_ID, FetchStatus.FETCHED) for e in WITH_DATA]


def test_fetch_game_with_force_refetches_every_endpoint(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_all(cache)
    get = _script(session, monkeypatch, *map(fixture_body, ENDPOINTS))

    results = nba_stats.fetch_game(session, cache, GAME_ID, force=True)

    assert len(get.calls) == 8
    assert [r.status for r in results] == [FetchStatus.FETCHED] * 7 + [FetchStatus.FAILED]


# The season-wide recordings of `record_fixtures.py stats-season`.
SEASON = "2025-26"
GSW = 1610612744
CURRY = 201939
REGULAR = SeasonType.REGULAR_SEASON

# nba_api's no-filter defaults for the league dashboards, written out as for the per-game params.
DASH = {
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
    "LeagueID": "00",
    "Location": "",
    "Outcome": "",
    "SeasonSegment": "",
    "VsConference": "",
    "VsDivision": "",
}
LEAGUE_DASH = {
    **DASH,
    "Conference": "",
    "Division": "",
    "PORound": "",
    "ShotClockRange": "",
    "TeamID": "",
}
STATS_DASH = {
    **LEAGUE_DASH,
    "GameScope": "",
    "PlayerExperience": "",
    "PlayerPosition": "",
    "StarterBench": "",
    "TwoWay": "",
}
PLAYER_STATS_ONLY = {
    "College": "",
    "Country": "",
    "DraftPick": "",
    "DraftYear": "",
    "Height": "",
    "Weight": "",
}
SEASON_ARGS = {"Season": SEASON, "SeasonType": "Regular Season"}

type SeasonFetch = Callable[[NbaSession, RawCache], dict[str, Any]]

# (endpoint, cache key, the params sent, the call), one per endpoint function and measure type.
SEASON_CASES: list[tuple[str, str, dict[str, str], SeasonFetch]] = [
    (
        "scoreboardv3",
        "2025-10-23",
        {"GameDate": "2025-10-23", "LeagueID": "00"},
        lambda s, c: nba_stats.fetch_scoreboard_v3(s, c, date(2025, 10, 23)),
    ),
    (
        "commonteamroster",
        f"{GSW}_{SEASON}",
        {"TeamID": str(GSW), "Season": SEASON, "LeagueID": "00"},
        lambda s, c: nba_stats.fetch_common_team_roster(s, c, GSW, SEASON),
    ),
    (
        "scheduleleaguev2",
        SEASON,
        {"LeagueID": "00", "Season": SEASON},
        lambda s, c: nba_stats.fetch_schedule_league_v2(s, c, SEASON),
    ),
    (
        "commonallplayers",
        SEASON,
        {"IsOnlyCurrentSeason": "0", "LeagueID": "00", "Season": SEASON},
        lambda s, c: nba_stats.fetch_common_all_players(s, c, SEASON),
    ),
    (
        "playergamelogs",
        f"{SEASON}_regular-season",
        {
            "DateFrom": "",
            "DateTo": "",
            "GameSegment": "",
            "LastNGames": "",
            "LeagueID": "00",
            "Location": "",
            "Month": "",
            "Outcome": "",
            "PORound": "",
            "PerMode": "",
            "Period": "",
            "PlayerID": "",
            "SeasonSegment": "",
            "ShotClockRange": "",
            "TeamID": "",
            "VsConference": "",
            "VsDivision": "",
            **SEASON_ARGS,
        },
        lambda s, c: nba_stats.fetch_player_game_logs(s, c, SEASON, REGULAR),
    ),
    (
        "shotchartdetail",
        f"{CURRY}_{SEASON}_regular-season",
        {
            "TeamID": "0",
            "PlayerID": str(CURRY),
            "ContextMeasure": "FGA",
            "LastNGames": "0",
            "LeagueID": "00",
            "Month": "0",
            "OpponentTeamID": "0",
            "Period": "0",
            "SeasonType": "Regular Season",
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
            "Season": SEASON,
            "SeasonSegment": "",
            "StartPeriod": "",
            "StartRange": "",
            "VsConference": "",
            "VsDivision": "",
        },
        lambda s, c: nba_stats.fetch_shot_chart_detail(s, c, CURRY, SEASON, REGULAR),
    ),
    (
        "leaguedashlineups",
        f"{SEASON}_regular-season",
        {**LEAGUE_DASH, "GroupQuantity": "5", "MeasureType": "Advanced", **SEASON_ARGS},
        lambda s, c: nba_stats.fetch_league_dash_lineups(s, c, SEASON, REGULAR),
    ),
    (
        "leaguedashlineups",
        f"{SEASON}_regular-season_last10",
        {
            **LEAGUE_DASH,
            "GroupQuantity": "5",
            "LastNGames": "10",
            "MeasureType": "Advanced",
            **SEASON_ARGS,
        },
        lambda s, c: nba_stats.fetch_league_dash_lineups(s, c, SEASON, REGULAR, 10),
    ),
    (
        "leagueplayerondetails",
        f"{GSW}_{SEASON}_regular-season",
        {**DASH, "TeamID": str(GSW), "MeasureType": "Advanced", **SEASON_ARGS},
        lambda s, c: nba_stats.fetch_league_player_on_details(s, c, GSW, SEASON, REGULAR),
    ),
    *(
        (
            "leaguedashplayerstats",
            f"{SEASON}_regular-season_{measure.lower()}",
            {**STATS_DASH, **PLAYER_STATS_ONLY, "MeasureType": measure, **SEASON_ARGS},
            lambda s, c, m=measure: nba_stats.fetch_league_dash_player_stats(
                s, c, SEASON, REGULAR, MeasureType(m)
            ),
        )
        for measure in ("Base", "Advanced")
    ),
    *(
        (
            "leaguedashteamstats",
            f"{SEASON}_regular-season_{measure.lower()}",
            {**STATS_DASH, "MeasureType": measure, **SEASON_ARGS},
            lambda s, c, m=measure: nba_stats.fetch_league_dash_team_stats(
                s, c, SEASON, REGULAR, MeasureType(m)
            ),
        )
        for measure in ("Base", "Advanced")
    ),
]
SEASON_IDS = [f"{endpoint}-{key}" for endpoint, key, _, _ in SEASON_CASES]
SEASON_ENDPOINTS = sorted({endpoint for endpoint, _, _, _ in SEASON_CASES})


def season_fixture_body(endpoint: str) -> bytes:
    """The endpoint's one recorded fixture, whatever key it was recorded under."""
    (path,) = (FIXTURES / endpoint).glob("*.json")
    return path.read_bytes()


def test_every_season_wide_endpoint_has_a_fixture() -> None:
    for endpoint in SEASON_ENDPOINTS:
        assert season_fixture_body(endpoint)


@pytest.mark.parametrize(("endpoint", "key", "params", "fetch"), SEASON_CASES, ids=SEASON_IDS)
def test_season_wide_fetch_sends_the_spec_params_and_caches_under_the_key(
    session: NbaSession,
    cache: RawCache,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    endpoint: str,
    key: str,
    params: dict[str, str],
    fetch: SeasonFetch,
) -> None:
    body = season_fixture_body(endpoint)
    get = _script(session, monkeypatch, body)

    assert fetch(session, cache) == json.loads(body)
    assert get.calls == [(f"https://stats.nba.com/stats/{endpoint}", params)]
    assert (tmp_path / "raw" / "nba_stats" / endpoint / f"{key}.json").read_bytes() == body


# The result-set endpoints; `scoreboardv3` and `scheduleleaguev2` are the season-wide endpoints in
# the v3 shape.
V3_SEASON_ENDPOINTS = {"scoreboardv3", "scheduleleaguev2"}
RESULT_SET_CASES = [case for case in SEASON_CASES if case[0] not in V3_SEASON_ENDPOINTS]
RESULT_SET_IDS = [f"{endpoint}-{key}" for endpoint, key, _, _ in RESULT_SET_CASES]


@pytest.mark.parametrize(
    ("endpoint", "key", "params", "fetch"),
    RESULT_SET_CASES,
    ids=RESULT_SET_IDS,
)
def test_a_season_wide_response_with_no_rows_is_not_cached(
    session: NbaSession,
    cache: RawCache,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    key: str,
    params: dict[str, str],
    fetch: SeasonFetch,
) -> None:
    # As stats.nba.com answers a season with no games yet, or a team-season that does not exist.
    _script(session, monkeypatch, json.dumps(_v2_shell(endpoint)).encode())

    with pytest.raises(NbaHttpError, match=f"{endpoint} has no data for {key}"):
        fetch(session, cache)
    assert not cache.exists("nba_stats", endpoint, key)


def test_a_schedule_with_no_games_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty: dict[str, Any] = {
        "meta": {},
        "leagueSchedule": {"seasonYear": "2026-27", "gameDates": []},
    }
    _script(session, monkeypatch, json.dumps(empty).encode())

    with pytest.raises(NbaHttpError, match="scheduleleaguev2 has no data for 2026-27"):
        nba_stats.fetch_schedule_league_v2(session, cache, "2026-27")
    assert not cache.exists("nba_stats", "scheduleleaguev2", "2026-27")


def test_a_schedule_of_another_season_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(session, monkeypatch, season_fixture_body("scheduleleaguev2"))

    with pytest.raises(NbaHttpError, match="scheduleleaguev2 has no data for 2024-25"):
        nba_stats.fetch_schedule_league_v2(session, cache, "2024-25")
    assert not cache.exists("nba_stats", "scheduleleaguev2", "2024-25")


def _scoreboard(day: str, *statuses: int) -> bytes:
    games = [{"gameId": f"00226000{i:02d}", "gameStatus": s} for i, s in enumerate(statuses)]
    return json.dumps({"meta": {}, "scoreboard": {"gameDate": day, "games": games}}).encode()


def test_a_scoreboard_with_a_game_not_final_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(session, monkeypatch, _scoreboard("2026-10-25", 3, 1))

    with pytest.raises(NbaHttpError, match="scoreboardv3 for 2026-10-25 has games not final"):
        nba_stats.fetch_scoreboard_v3(session, cache, date(2026, 10, 25))
    assert not cache.exists("nba_stats", "scoreboardv3", "2026-10-25")


def test_a_scoreboard_of_a_date_with_no_games_is_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(session, monkeypatch, _scoreboard("2026-07-04"))

    nba_stats.fetch_scoreboard_v3(session, cache, date(2026, 7, 4))

    assert cache.exists("nba_stats", "scoreboardv3", "2026-07-04")


def test_a_scoreboard_of_another_date_is_not_cached(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(session, monkeypatch, _scoreboard("2026-10-24", 3))

    with pytest.raises(NbaHttpError, match="scoreboardv3 has no data for 2026-10-25"):
        nba_stats.fetch_scoreboard_v3(session, cache, date(2026, 10, 25))


def _requested(get: ScriptedGet) -> list[tuple[str, dict[str, str] | None]]:
    """Each request's endpoint and params, the URL's base stripped."""
    return [(url.removeprefix("https://stats.nba.com/stats/"), params) for url, params in get.calls]


def test_fetch_rosters_fetches_every_team_then_every_player(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = season_fixture_body("commonteamroster")
    get = _script(session, monkeypatch, *[roster] * 30, season_fixture_body("commonallplayers"))

    results = nba_stats.fetch_rosters(session, cache, "2026-27")

    assert _requested(get) == [
        ("commonteamroster", {"TeamID": str(t.nba_team_id), "Season": "2026-27", "LeagueID": "00"})
        for t in TEAMS
    ] + [("commonallplayers", {"IsOnlyCurrentSeason": "0", "LeagueID": "00", "Season": "2026-27"})]
    assert results == [
        EndpointResult("commonteamroster", f"{t.nba_team_id}_2026-27", FetchStatus.FETCHED)
        for t in TEAMS
    ] + [EndpointResult("commonallplayers", "2026-27", FetchStatus.FETCHED)]


def test_fetch_aggregates_fetches_each_league_wide_pull_and_every_team_on_off(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoints = [
        "playergamelogs",
        "leaguedashlineups",
        "leaguedashplayerstats",
        "leaguedashplayerstats",
        "leaguedashteamstats",
        "leaguedashteamstats",
        *["leagueplayerondetails"] * 30,
    ]
    _script(session, monkeypatch, *map(season_fixture_body, endpoints))

    results = nba_stats.fetch_aggregates(session, cache, SEASON, REGULAR)

    assert [(r.endpoint, r.key) for r in results] == [
        ("playergamelogs", f"{SEASON}_regular-season"),
        ("leaguedashlineups", f"{SEASON}_regular-season"),
        ("leaguedashplayerstats", f"{SEASON}_regular-season_base"),
        ("leaguedashplayerstats", f"{SEASON}_regular-season_advanced"),
        ("leaguedashteamstats", f"{SEASON}_regular-season_base"),
        ("leaguedashteamstats", f"{SEASON}_regular-season_advanced"),
    ] + [("leagueplayerondetails", f"{t.nba_team_id}_{SEASON}_regular-season") for t in TEAMS]
    assert {r.status for r in results} == {FetchStatus.FETCHED}


def test_fetch_player_shots_reports_a_player_with_no_shots_as_failed(
    session: NbaSession, cache: RawCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(session, monkeypatch, json.dumps(_v2_shell("shotchartdetail")).encode())

    results = nba_stats.fetch_player_shots(session, cache, CURRY, "2026-27", SeasonType.PLAYOFFS)

    key = f"{CURRY}_2026-27_playoffs"
    assert results == [
        EndpointResult(
            "shotchartdetail", key, FetchStatus.FAILED, f"shotchartdetail has no data for {key}"
        )
    ]

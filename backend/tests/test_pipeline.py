import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from closinglineup.clock import FakeClock
from closinglineup.load.parquet import ParquetStore
from closinglineup.pipeline import NotCachedError, game_date_of, parse_date
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from tests.test_nba_stats import WITH_DATA, fixture_body, season_fixture_body

TZ = ZoneInfo("America/New_York")
# DEN at GSW, the stats.nba.com fixture game, beside a final game with nothing cached.
GAME = "0022500006"
UNCACHED = "0022500007"
DAY = date(2025, 10, 23)
GSW, DEN, LAL, MIN = 1610612744, 1610612743, 1610612747, 1610612750
# 23:30 on the 23rd in New York, already the 24th in UTC.
ROSTERS_FETCHED = datetime(2025, 10, 24, 3, 30, tzinfo=UTC)
# 13 GSW and 15 DEN players in the traditional box; 10 players in the trimmed player list, none of
# them among GSW's 18 on its roster.
PLAYER_GAMES = 28
PLAYERS = 28
GSW_ROSTER = 18

type Payload = dict[str, Any]


def _schedule_game(game_id: str, home: int, away: int, score: tuple[int, int]) -> Payload:
    return {
        "gameId": game_id,
        "gameStatus": 3,
        "gameSubtype": "",
        "gameDateEst": f"{DAY.isoformat()}T00:00:00Z",
        "postponedStatus": "N",
        "homeTeam": {"teamId": home, "score": score[0]},
        "awayTeam": {"teamId": away, "score": score[1]},
    }


# The 2025-26 schedule fixture is trimmed, and the fixture game is not in it.
SCHEDULE: Payload = {
    "leagueSchedule": {
        "seasonYear": "2025-26",
        "gameDates": [
            {
                "gameDate": "10/23/2025 00:00:00",
                "games": [
                    _schedule_game(GAME, GSW, DEN, (137, 131)),
                    _schedule_game(UNCACHED, LAL, MIN, (110, 100)),
                ],
            }
        ],
    }
}


def cache_fixture_game(data_dir: Path, *, rosters: bool = True) -> RawCache:
    """The fixture game's schedule and box scores in a raw cache under `data_dir`, and with
    `rosters`, the GSW roster and the player list."""
    cache = RawCache(data_dir, FakeClock(ROSTERS_FETCHED))
    cache.put(
        nba_cdn.SOURCE,
        nba_cdn.SCHEDULE,
        "2025-26",
        json.dumps(SCHEDULE).encode(),
        url=nba_cdn.SCHEDULE_URL,
        status=200,
    )
    for endpoint in WITH_DATA:
        cache.put(nba_stats.SOURCE, endpoint, GAME, fixture_body(endpoint), url="u", status=200)
    if rosters:
        for endpoint, key in [
            (nba_stats.COMMON_TEAM_ROSTER, f"{GSW}_2025-26"),
            (nba_stats.COMMON_ALL_PLAYERS, "2025-26"),
        ]:
            cache.put(
                nba_stats.SOURCE, endpoint, key, season_fixture_body(endpoint), url="u", status=200
            )
    return cache


def test_a_date_is_parsed_into_a_file_per_table(tmp_path: Path) -> None:
    cache = cache_fixture_game(tmp_path)
    store = ParquetStore(tmp_path)

    report = parse_date(cache, store, DAY, TZ)

    assert report.parsed == [GAME]
    assert report.missing == [UNCACHED]
    assert report.failed == {}
    assert report.rows == {
        "game": 2,
        "player": PLAYERS,
        "roster_membership": GSW_ROSTER,
        "player_game": PLAYER_GAMES,
        "matchup": store.read("matchup", DAY).num_rows,
    }
    assert report.rows["matchup"] > 0
    assert set(store.read("player_game", DAY).column("game_id").to_pylist()) == {GAME}


def test_a_roster_opens_on_the_day_it_was_fetched_in_tz(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)

    parse_date(cache_fixture_game(tmp_path), store, DAY, TZ)

    starts = set(store.read("roster_membership", DAY).column("start_date").to_pylist())
    assert starts == {date(2025, 10, 23)}


def test_another_seasons_rosters_are_left_out(tmp_path: Path) -> None:
    cache = cache_fixture_game(tmp_path, rosters=False)
    body = season_fixture_body(nba_stats.COMMON_TEAM_ROSTER)
    key = f"{GSW}_2026-27"
    cache.put(nba_stats.SOURCE, nba_stats.COMMON_TEAM_ROSTER, key, body, url="u", status=200)

    report = parse_date(cache, ParquetStore(tmp_path), DAY, TZ)

    assert (report.rows["player"], report.rows["roster_membership"]) == (0, 0)


def test_a_game_a_parser_cannot_read_is_reported_and_left_out(tmp_path: Path) -> None:
    cache = cache_fixture_game(tmp_path)
    box = json.loads(fixture_body(nba_stats.BOXSCORE_TRADITIONAL_V3))
    # The fixture game's box score, cached under another game's ID.
    cache.put(
        nba_stats.SOURCE,
        nba_stats.BOXSCORE_TRADITIONAL_V3,
        UNCACHED,
        json.dumps(box).encode(),
        url="u",
        status=200,
    )

    report = parse_date(cache, ParquetStore(tmp_path), DAY, TZ)

    assert report.parsed == [GAME]
    assert report.failed == {
        UNCACHED: f"ParseError: cached box scores of {UNCACHED} are for game {GAME}"
    }
    assert report.rows["player_game"] == PLAYER_GAMES


def test_a_date_with_no_games_writes_empty_files(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)

    report = parse_date(cache_fixture_game(tmp_path), store, date(2025, 10, 22), TZ)

    assert report.parsed == report.missing == []
    assert report.rows["game"] == report.rows["player_game"] == 0
    assert store.read("game", date(2025, 10, 22)).num_rows == 0


def test_a_season_with_no_cached_schedule_raises(tmp_path: Path) -> None:
    with pytest.raises(NotCachedError, match="no 2025-26 schedule in the raw cache"):
        parse_date(RawCache(tmp_path), ParquetStore(tmp_path), DAY, TZ)


def test_a_games_date_comes_from_its_seasons_schedule(tmp_path: Path) -> None:
    cache = cache_fixture_game(tmp_path)

    assert game_date_of(cache, GAME) == DAY
    with pytest.raises(NotCachedError, match="0022500999 is not in"):
        game_date_of(cache, "0022500999")

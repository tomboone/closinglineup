import time as clock
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.api.app import create_app
from closinglineup.db.models import (
    Game,
    ImpactSnapshot,
    Player,
    PlayerGame,
    RosterMembership,
    RotationState,
)
from closinglineup.derive.windows import refresh_windows
from closinglineup.settings import Settings

# Seasons no other test seeds.
SEASON = "2038-39"
PRIOR_SEASON = "2037-38"
FIRST = date(2038, 11, 1)
PRIOR_DAY = date(2038, 3, 1)
PRIOR_END = date(2038, 4, 12)
BOS = 1610612738
NYK = 1610612752
PHI = 1610612755
CHA = 1610612766
# BOS and NYK play games 1 to 12, one a day; PHI and CHA games 1 to 11, so they are idle on the
# last date.
GAMES = range(1, 13)
IDLE_GAMES = range(1, 12)
# BOS's star scores 20 on 10 shots in games 1 and 2 and 10 after; everyone else 10 on 10.
STAR = 9_600_001
BOS_FOUR = [9_600_002, 9_600_003, 9_600_004, 9_600_005]
NYK_FIVE = [9_600_011, 9_600_012, 9_600_013, 9_600_014, 9_600_015]
# PHI's, idle on the last date.
IDLE = 9_600_021
PHI_FOUR = [9_600_022, 9_600_023, 9_600_024, 9_600_025]
CHA_FIVE = [9_600_031, 9_600_032, 9_600_033, 9_600_034, 9_600_035]
# PHI's in the prior season's one game, against BOS, and on no roster since.
VETERAN = 9_600_041
CURRENT = [STAR, *BOS_FOUR, *NYK_FIVE, IDLE, *PHI_FOUR, *CHA_FIVE]
PLAYERS = [*CURRENT, VETERAN]
PRIOR_GAME = "0023700001"


def _day(g: int) -> date:
    return FIRST + timedelta(days=g - 1)


LAST = _day(GAMES[-1])


def _game_id(g: int, home: int) -> str:
    return f"00238{g + (100 if home == PHI else 0):05d}"


def _game(game_id: str, season: str, day: date, home: int, away: int) -> dict[str, Any]:
    return {
        "game_id": game_id,
        "season": season,
        "game_date": day,
        "phase": "regular",
        "home_team_id": home,
        "away_team_id": away,
        "status": "final",
        "home_score": 100,
        "away_score": 95,
        "home_b2b": False,
        "away_b2b": False,
    }


def _line(game_id: str, player_id: int, team_id: int, pts: int = 10) -> dict[str, Any]:
    """48 minutes, `pts` on 10 field goal attempts."""
    counts = ("fgm", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl")
    return {
        "game_id": game_id,
        "player_id": player_id,
        "team_id": team_id,
        "is_starter": True,
        "seconds": 2880,
        "dnp_reason": None,
        **dict.fromkeys((*counts, "blk", "tov", "pf", "plus_minus"), 0),
        "pts": pts,
        "fga": 10,
        "ts_pct": None,
        "usg_pct": None,
        "source": "stats",
    }


def _lines(g: int) -> list[dict[str, Any]]:
    bos, phi = _game_id(g, BOS), _game_id(g, PHI)
    return [
        _line(bos, STAR, BOS, pts=20 if g <= 2 else 10),
        *(_line(bos, p, BOS) for p in BOS_FOUR),
        *(_line(bos, p, NYK) for p in NYK_FIVE),
        *(
            line
            for line in (
                _line(phi, IDLE, PHI),
                *(_line(phi, p, PHI) for p in PHI_FOUR),
                *(_line(phi, p, CHA) for p in CHA_FIVE),
            )
            if g in IDLE_GAMES
        ),
    ]


def _membership(player_id: int, team_id: int, end: date | None = None) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "start_date": PRIOR_DAY - timedelta(days=30),
        "end_date": end,
        "contract_type": "standard",
        "source": "player_game",
    }


def _snapshot(player_id: int, day: date, source: str, value: float) -> dict[str, Any]:
    return {"date": day, "player_id": player_id, "source": source, "value": value}


def _state(player_id: int, team_id: int, day: date, tier: str) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "game_date": day,
        "tier": tier,
        "availability": "active",
        "badges": [],
        "is_short": False,
        "inputs": {},
    }


def clear(db: sessionmaker[Session]) -> None:
    game_ids = [
        PRIOR_GAME,
        *(_game_id(g, BOS) for g in GAMES),
        *(_game_id(g, PHI) for g in IDLE_GAMES),
    ]
    with db.begin() as session:
        for model in (RotationState, ImpactSnapshot, RosterMembership):
            session.execute(delete(model).where(model.player_id.in_(PLAYERS)))
        session.execute(delete(Player).where(Player.nba_player_id.in_(PLAYERS)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """Two pairings' games, one idle on the last date, a prior-season game, impact and value
    snapshots, and rotation states; the view refreshed."""
    clear(db)
    with db.begin() as session:
        session.execute(
            insert(Game),
            [
                _game(PRIOR_GAME, PRIOR_SEASON, PRIOR_DAY, BOS, PHI),
                *(_game(_game_id(g, BOS), SEASON, _day(g), BOS, NYK) for g in GAMES),
                *(_game(_game_id(g, PHI), SEASON, _day(g), PHI, CHA) for g in IDLE_GAMES),
            ],
        )
        session.execute(
            insert(PlayerGame),
            [
                _line(PRIOR_GAME, STAR, BOS, pts=15),
                *(_line(PRIOR_GAME, p, BOS) for p in BOS_FOUR),
                _line(PRIOR_GAME, VETERAN, PHI, pts=12),
                *(_line(PRIOR_GAME, p, PHI) for p in PHI_FOUR),
                *(line for g in GAMES for line in _lines(g)),
            ],
        )
        session.execute(
            insert(RosterMembership),
            [
                *(_membership(p, BOS) for p in (STAR, *BOS_FOUR)),
                *(_membership(p, NYK) for p in NYK_FIVE),
                *(_membership(p, PHI) for p in (IDLE, *PHI_FOUR)),
                *(_membership(p, CHA) for p in CHA_FIVE),
                _membership(VETERAN, PHI, PRIOR_END),
            ],
        )
        session.execute(
            insert(Player), [{"nba_player_id": p, "name": f"Player {p}"} for p in PLAYERS]
        )
        session.execute(
            insert(ImpactSnapshot),
            [
                _snapshot(STAR, _day(1), "darko", 1.5),
                _snapshot(STAR, _day(1), "bpm_local", 0.5),
                _snapshot(IDLE, _day(1), "bpm_local", -0.5),
                _snapshot(STAR, _day(2), "ws_local", 0.4),
                _snapshot(STAR, _day(12), "ws_local", 2.0),
                _snapshot(STAR, _day(12), "vorp_local", 0.5),
                _snapshot(VETERAN, PRIOR_DAY, "ws_local", 0.3),
                _snapshot(VETERAN, PRIOR_DAY, "vorp_local", 0.2),
                # After his last game: the season's final snapshots.
                _snapshot(VETERAN, PRIOR_END, "ws_local", 0.9),
                _snapshot(VETERAN, PRIOR_END, "vorp_local", 0.25),
            ],
        )
        session.execute(
            insert(RotationState),
            [
                _state(STAR, BOS, LAST, "starter"),
                _state(IDLE, PHI, _day(10), "fringe"),
                _state(IDLE, PHI, _day(11), "core"),
                # Last season's: no tier this season.
                _state(VETERAN, PHI, PRIOR_DAY, "starter"),
            ],
        )
    refresh_windows(db)
    yield db
    clear(db)
    refresh_windows(db)


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        yield client


def _table(client: TestClient, **params: str) -> dict[str, Any]:
    response = client.get("/api/players", params={"date": LAST.isoformat(), **params})
    assert response.status_code == 200, response.text
    return response.json()


def _rows(table: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {row["player"]["player_id"]: row for row in table["rows"]}


def test_every_player_with_a_game_this_season_has_one_row(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    table = _table(client)

    assert (table["date"], table["window"]) == (LAST.isoformat(), "season")
    assert sorted(row["player"]["player_id"] for row in table["rows"]) == sorted(CURRENT)
    star = _rows(table)[STAR]
    assert star["player"]["name"] == f"Player {STAR}"
    assert (star["tricode"], star["tier"]) == ("BOS", "starter")
    assert (star["games"], star["games_played"]) == (12, 12)
    assert star["minutes_per_game"] == pytest.approx(48.0)
    # 140 points on 120 shots; Win Shares are the season-to-date snapshot, over 12 games of 48.
    assert star["ts_pct"] == pytest.approx(140 / 240)
    assert star["ws"] == pytest.approx(2.0)
    assert star["ws_48"] == pytest.approx(2.0 / 12)
    assert star["vorp"] == pytest.approx(0.5)
    assert star["game_score"] == pytest.approx((140 - 0.7 * 120) / 12)
    # DARKO before local BPM.
    assert star["impact"] == {"source": "darko", "value": 1.5}
    nyk = _rows(table)[NYK_FIVE[0]]
    assert (nyk["tier"], nyk["tricode"], nyk["impact"], nyk["ws"]) == (None, None, None, None)


def test_a_player_whose_team_was_idle_on_the_date_has_his_team_s_last_game_s_row(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    idle = _rows(_table(client))[IDLE]

    assert (idle["games"], idle["games_played"]) == (11, 11)
    assert idle["ts_pct"] == pytest.approx(0.5)
    assert (idle["tricode"], idle["tier"]) == ("PHI", "core")
    assert idle["impact"] == {"source": "bpm_local", "value": -0.5}


def test_window_10_returns_the_10_game_values(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    table = _table(client, window="10")
    star = _rows(table)[STAR]

    assert table["window"] == "10"
    # Games 3 to 12: 10 on 10 each; Win Shares at game 12 less those at game 2.
    assert (star["games"], star["games_played"]) == (10, 10)
    assert star["ts_pct"] == pytest.approx(0.5)
    assert star["ws"] == pytest.approx(1.6)
    assert star["ws_48"] == pytest.approx(1.6 / 10)


@pytest.mark.parametrize("window", ["5", "10", "20"])
def test_vorp_is_null_in_the_game_windows(
    seeded: sessionmaker[Session], client: TestClient, window: str
) -> None:
    assert all(row["vorp"] is None for row in _table(client, window=window)["rows"])


def test_prior_season_is_last_season_s_whole_including_a_player_with_no_game_this_season(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    table = _table(client, window="prior_season")
    rows = _rows(table)

    assert table["window"] == "prior_season"
    # PHI's idle player was on its roster for the game but has no line in it.
    assert sorted(rows) == sorted([STAR, *BOS_FOUR, VETERAN, IDLE, *PHI_FOUR])
    assert (rows[IDLE]["games"], rows[IDLE]["games_played"]) == (1, 0)
    veteran = rows[VETERAN]
    assert (veteran["games"], veteran["games_played"]) == (1, 1)
    assert veteran["ts_pct"] == pytest.approx(12 / 20)
    # The season's final value snapshots, as the player page's prior season reads them, though
    # they postdate his last game; WS/48 over his 48 minutes.
    assert (veteran["ws"], veteran["vorp"]) == (pytest.approx(0.9), pytest.approx(0.25))
    assert veteran["ws_48"] == pytest.approx(0.9)
    # His tier and team are this season's, and he has none.
    assert (veteran["tricode"], veteran["tier"]) == (None, None)
    assert rows[STAR]["ts_pct"] == pytest.approx(15 / 20)
    assert rows[STAR]["tier"] == "starter"


def test_the_league_table_answers_within_two_seconds(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    started = clock.perf_counter()
    response = client.get("/api/players", params={"date": LAST.isoformat()})
    elapsed = clock.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 2.0

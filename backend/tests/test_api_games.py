import csv
import gzip
from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.api.app import create_app
from closinglineup.db.models import Game, Player, PlayerGame, Stint
from closinglineup.settings import Settings

# A real game, OKC's 125-124 overtime win over HOU on opening night 2025-26, from the derive
# fixture; loaded under an ID of its own so it never meets a real one already in the database.
FIXTURES = Path(__file__).parent / "fixtures" / "derive"
REAL_GAME = "0022500001"
FIXTURE_GAME = "0024299001"

SEASON = "2042-43"
BOS = 1610612738
NYK = 1610612752
BOS_FIVE = [9_400_001, 9_400_002, 9_400_003, 9_400_004, 9_400_005]
BOS_BENCH = [9_400_006, 9_400_007]
NYK_FIVE = [9_400_011, 9_400_012, 9_400_013, 9_400_014, 9_400_015]
PLAYERS = [*BOS_FIVE, *BOS_BENCH, *NYK_FIVE]
# BOS by 20 and by 6.
BLOWOUT = "0024200001"
CLOSE = "0024200002"
GAMES = [BLOWOUT, CLOSE, FIXTURE_GAME]


def _key(players: list[int]) -> str:
    return "-".join(str(p) for p in sorted(players))


def _game(game_id: str, home_score: int, away_score: int) -> dict[str, Any]:
    return {
        "game_id": game_id,
        "season": SEASON,
        "game_date": date(2042, 11, 1),
        "phase": "regular",
        "home_team_id": BOS,
        "away_team_id": NYK,
        "status": "final",
        "home_score": home_score,
        "away_score": away_score,
        "home_b2b": False,
        "away_b2b": False,
    }


def _stint(
    game_id: str,
    period: int,
    start: float,
    end: float,
    five: list[int],
    poss: int,
    scored: int,
    allowed: int,
    garbage: bool = False,
) -> list[dict[str, Any]]:
    """BOS's view of a stint and NYK's, NYK a possession short."""
    return [
        {
            "game_id": game_id,
            "period": period,
            "start_sec": start,
            "end_sec": end,
            "team_id": team_id,
            "lineup_key": key,
            "opp_lineup_key": opp_key,
            "possessions": possessions,
            "pts_for": pts_for,
            "pts_against": pts_against,
            "is_garbage_time": garbage,
        }
        for team_id, key, opp_key, possessions, pts_for, pts_against in (
            (BOS, _key(five), _key(NYK_FIVE), poss, scored, allowed),
            (NYK, _key(NYK_FIVE), _key(five), poss - 1, allowed, scored),
        )
    ]


SECOND_UNIT = [*BOS_FIVE[:3], *BOS_BENCH]


def _stints(game_id: str, garbage: bool) -> list[dict[str, Any]]:
    """The starters play the first 600 seconds of each period but the fourth, 20-15 on 20; the
    second unit the rest, 4-2 on 4; in the blowout the fourth's last 120 seconds are garbage
    time."""
    rows: list[dict[str, Any]] = []
    for period in (1, 2, 3, 4):
        rows += _stint(game_id, period, 0.0, 600.0, BOS_FIVE, 20, 20, 15)
        if period == 4 and garbage:
            rows += _stint(game_id, period, 600.0, 720.0, SECOND_UNIT, 4, 4, 2, garbage=True)
        else:
            rows += _stint(game_id, period, 600.0, 720.0, SECOND_UNIT, 4, 4, 2)
    return rows


def _line(
    game_id: str, player_id: int, team_id: int, seconds: int, starter: bool
) -> dict[str, Any]:
    counts = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl")
    return {
        "game_id": game_id,
        "player_id": player_id,
        "team_id": team_id,
        "is_starter": starter,
        "seconds": seconds,
        "dnp_reason": None if seconds else "cd",
        **dict.fromkeys((*counts, "blk", "tov", "pf", "plus_minus"), 0),
        "source": "stats",
    }


def _lines(game_id: str) -> list[dict[str, Any]]:
    return [
        *(_line(game_id, p, BOS, 2880, True) for p in BOS_FIVE[:3]),
        *(_line(game_id, p, BOS, 2400, True) for p in BOS_FIVE[3:]),
        *(_line(game_id, p, BOS, 480, False) for p in BOS_BENCH),
        *(_line(game_id, p, NYK, 2880, True) for p in NYK_FIVE),
    ]


def _fixture_rows(table: str) -> list[dict[str, Any]]:
    """The real game's rows of `table` from the derive fixture, under `FIXTURE_GAME`, the CSV's
    empty fields as NULL."""
    with gzip.open(FIXTURES / f"{table}.csv.gz", "rt") as rows:
        return [
            {
                **{column: value if value != "" else None for column, value in row.items()},
                "game_id": FIXTURE_GAME,
            }
            for row in csv.DictReader(rows)
            if row["game_id"] == REAL_GAME
        ]


def _typed(row: dict[str, Any], model: type[Game | PlayerGame | Stint]) -> dict[str, Any]:
    """CSV text as the column types: booleans from Postgres's `t`/`f`, the rest by SQLAlchemy."""
    columns = model.__table__.columns  # pyright: ignore[reportAttributeAccessIssue]
    typed: dict[str, Any] = {}
    for name, value in row.items():
        python_type = columns[name].type.python_type
        if value is None:
            typed[name] = None
        elif python_type is bool:
            typed[name] = value == "t"
        elif python_type is date:
            typed[name] = date.fromisoformat(value)
        else:
            typed[name] = python_type(value)
    return typed


def clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(Stint).where(Stint.game_id.in_(GAMES)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(GAMES)))
        session.execute(delete(Game).where(Game.game_id.in_(GAMES)))
        session.execute(delete(Player).where(Player.nba_player_id.in_(PLAYERS)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """BOS's 20-point and 6-point wins over NYK, the first with garbage time, and the fixture
    game."""
    clear(db)
    with db.begin() as session:
        session.execute(
            insert(Game),
            [
                _game(BLOWOUT, 110, 90),
                _game(CLOSE, 100, 94),
                *(_typed(row, Game) for row in _fixture_rows("game")),
            ],
        )
        session.execute(
            insert(PlayerGame),
            [
                *_lines(BLOWOUT),
                *_lines(CLOSE),
                *(_typed(row, PlayerGame) for row in _fixture_rows("player_game")),
            ],
        )
        session.execute(
            insert(Stint),
            [
                *_stints(BLOWOUT, garbage=True),
                *_stints(CLOSE, garbage=False),
                *(_typed(row, Stint) for row in _fixture_rows("stint")),
            ],
        )
        session.execute(
            insert(Player), [{"nba_player_id": p, "name": f"Player {p}"} for p in PLAYERS]
        )
    yield db
    clear(db)


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        yield client


def test_every_player_with_minutes_has_stints_summing_to_his_seconds(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    response = client.get(f"/api/games/{FIXTURE_GAME}")

    assert response.status_code == 200
    page = response.json()
    on_floor: dict[int, float] = defaultdict(float)
    for stint in page["stints"]:
        on_floor[stint["player_id"]] += stint["end_sec"] - stint["start_sec"]
    players = [p for side in ("home", "away") for p in page[side]["players"]]
    assert players
    for player in players:
        # The box score rounds to the second.
        assert on_floor[player["player"]["player_id"]] == pytest.approx(player["seconds"], abs=0.5)


def test_stints_are_one_row_per_player_with_his_team_and_garbage_time(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = client.get(f"/api/games/{BLOWOUT}").json()

    assert len(page["stints"]) == 8 * 2 * 5
    assert page["stints"][0] == {
        "player_id": BOS_FIVE[0],
        "team_id": BOS,
        "period": 1,
        "start_sec": 0.0,
        "end_sec": 600.0,
        "is_garbage_time": False,
    }
    garbage = {s["player_id"] for s in page["stints"] if s["is_garbage_time"]}
    assert garbage == {*SECOND_UNIT, *NYK_FIVE}
    assert page["garbage_time_from"] == {"period": 4, "start_sec": 600.0}


def test_players_are_the_box_score_s_starters_first(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    home = client.get(f"/api/games/{CLOSE}").json()["home"]

    assert [p["player"]["player_id"] for p in home["players"]] == [*BOS_FIVE, *BOS_BENCH]
    assert home["players"][0] == {
        "player": {"player_id": BOS_FIVE[0], "name": f"Player {BOS_FIVE[0]}"},
        "is_starter": True,
        "seconds": 2880,
    }


def test_closing_lineup_is_null_for_a_20_point_game_and_the_last_five_for_a_6_point_one(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    blowout = client.get(f"/api/games/{BLOWOUT}").json()
    close = client.get(f"/api/games/{CLOSE}").json()

    assert blowout["home"]["closing"] is None
    assert blowout["away"]["closing"] is None
    assert [p["player_id"] for p in close["home"]["closing"]] == sorted(SECOND_UNIT)
    assert [p["player_id"] for p in close["away"]["closing"]] == NYK_FIVE
    assert close["garbage_time_from"] is None


def test_lineups_are_the_game_s_fives_by_possessions_with_net_rating(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    home = client.get(f"/api/games/{CLOSE}").json()["home"]

    assert [[p["player_id"] for p in lineup["players"]] for lineup in home["lineups"]] == [
        BOS_FIVE,
        sorted(SECOND_UNIT),
    ]
    starters = home["lineups"][0]
    assert (starters["possessions"], starters["opp_possessions"]) == (80, 76)
    assert (starters["pts_for"], starters["pts_against"]) == (80, 60)
    assert starters["net_rating"] == pytest.approx(100 * 80 / 80 - 100 * 60 / 76)


def test_lineups_leave_garbage_time_out_unless_included(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    excluded = client.get(f"/api/games/{BLOWOUT}").json()
    included = client.get(f"/api/games/{BLOWOUT}", params={"garbage_time": "include"}).json()

    assert excluded["garbage_time"] == "exclude"
    assert excluded["home"]["lineups"][1]["possessions"] == 12
    assert included["home"]["lineups"][1]["possessions"] == 16
    # The timeline shows every stint either way.
    assert len(excluded["stints"]) == len(included["stints"])


def test_an_unknown_game_is_404(seeded: sessionmaker[Session], client: TestClient) -> None:
    assert client.get("/api/games/0029999999").status_code == 404

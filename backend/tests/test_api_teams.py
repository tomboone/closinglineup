import time as clock
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.api.app import create_app
from closinglineup.api.routers.teams import StintRow, top_groups
from closinglineup.db.models import (
    Contract,
    Game,
    ImpactSnapshot,
    InjuryStatus,
    LoadLog,
    Player,
    PlayerGame,
    RosterMembership,
    RotationState,
    Stint,
)
from closinglineup.derive.rotation import TierRule, compute_rotation_state
from closinglineup.derive.run import WRITES
from closinglineup.derive.windows import refresh_windows
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN

# A season no other test seeds, so the latest derived date is this file's.
SEASON = "2030-31"
FIRST = date(2030, 11, 1)
GAMES = range(1, 23)
BOS = 1610612738
NYK = 1610612752
STARTERS = [9_300_001, 9_300_002, 9_300_003, 9_300_004, 9_300_005]
BENCH = [9_300_006, 9_300_007, 9_300_008]
# A two-way player who never gets off the bench, and one on G League assignment.
TWO_WAY = 9_300_009
ASSIGNED = 9_300_010
NYK_FIVE = [9_300_011, 9_300_012, 9_300_013, 9_300_014, 9_300_015]
# On a contract with the team but no longer on its roster.
WAIVED = 9_300_016
BOS_ROSTER = [*STARTERS, *BENCH, TWO_WAY, ASSIGNED]
PLAYERS = [*BOS_ROSTER, *NYK_FIVE, WAIVED]
GAME_SECONDS = 14_400


def _game_id(g: int) -> str:
    return f"00230{g:05d}"


def _day(g: int) -> date:
    return FIRST + timedelta(days=2 * (g - 1))


LAST = _day(GAMES[-1])


def _key(players: list[int]) -> str:
    return "-".join(str(p) for p in sorted(players))


def _line(g: int, player_id: int, team_id: int, seconds: int, **values: Any) -> dict[str, Any]:
    counts = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl")
    return {
        "game_id": _game_id(g),
        "player_id": player_id,
        "team_id": team_id,
        "is_starter": False,
        "seconds": seconds,
        "dnp_reason": None,
        **dict.fromkeys((*counts, "blk", "tov", "pf", "plus_minus"), 0),
        "source": "stats",
        **values,
    }


def _lines(g: int) -> list[dict[str, Any]]:
    return [
        *(_line(g, p, BOS, 2400, is_starter=True) for p in STARTERS),
        *(_line(g, p, BOS, 800) for p in BENCH),
        _line(g, TWO_WAY, BOS, 0, dnp_reason="cd"),
        *(_line(g, p, NYK, 2880, is_starter=True) for p in NYK_FIVE),
    ]


def _stint(
    g: int,
    period: int,
    start: float,
    five: list[int],
    poss: int,
    scored: int,
    allowed: int,
    garbage: bool = False,
) -> list[dict[str, Any]]:
    """BOS's view of a stint and NYK's."""
    return [
        {
            "game_id": _game_id(g),
            "period": period,
            "start_sec": start,
            "end_sec": start + 120 if period == 4 else 720.0,
            "team_id": team_id,
            "lineup_key": key,
            "opp_lineup_key": opp_key,
            "possessions": poss,
            "pts_for": pts_for,
            "pts_against": pts_against,
            "is_garbage_time": garbage,
        }
        for team_id, key, opp_key, pts_for, pts_against in (
            (BOS, _key(five), _key(NYK_FIVE), scored, allowed),
            (NYK, _key(NYK_FIVE), _key(five), allowed, scored),
        )
    ]


CLOSERS = [*STARTERS[:3], *BENCH[:2]]


def _stints(g: int) -> list[dict[str, Any]]:
    """The starters win the first quarter 30-20 on 25 possessions a side and three of them close
    with two of the bench, 10-5 on 5; in the last game the starters close instead, 0-20 on 10 in
    garbage time."""
    rows = _stint(g, 1, 0.0, STARTERS, 25, 30, 20)
    if g == GAMES[-1]:
        return rows + _stint(g, 4, 600.0, STARTERS, 10, 0, 20, garbage=True)
    return rows + _stint(g, 4, 600.0, CLOSERS, 5, 10, 5)


def _game(g: int) -> dict[str, Any]:
    """BOS wins the odd games by 5, the even ones by 30."""
    return {
        "game_id": _game_id(g),
        "season": SEASON,
        "game_date": _day(g),
        "phase": "regular",
        "home_team_id": BOS,
        "away_team_id": NYK,
        "status": "final",
        "home_score": 100 if g % 2 else 120,
        "away_score": 95 if g % 2 else 90,
        "home_b2b": False,
        "away_b2b": False,
    }


def _membership(player_id: int, team_id: int, contract_type: str = "standard") -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "start_date": FIRST - timedelta(days=30),
        "end_date": None,
        "contract_type": contract_type,
        "source": "player_game",
    }


def _injury(
    player_id: int, team_id: int, at: datetime, status: str, reason: str, source: str = "pdf"
) -> dict[str, Any]:
    return {
        "report_datetime": at,
        "game_date": at.date(),
        "player_id": player_id,
        "team_id": team_id,
        "status": status,
        "reason": reason,
        "source": source,
    }


def _contract(player_id: int, guarantee_date: date) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": BOS,
        "season": SEASON,
        "salary": None,
        "minimum_salary": True,
        "guaranteed": 0,
        "guarantee_date": guarantee_date,
        "guaranteed_after": None,
        "as_of_date": LAST - timedelta(days=3),
        "source": "hoopsrumors",
    }


def _value(
    day: date,
    player_id: int,
    source: str,
    value: float,
    o_value: float | None = None,
    d_value: float | None = None,
) -> dict[str, Any]:
    return {
        "date": day,
        "player_id": player_id,
        "source": source,
        "value": value,
        "o_value": o_value,
        "d_value": d_value,
    }


def clear(db: sessionmaker[Session]) -> None:
    game_ids = [_game_id(g) for g in GAMES]
    with db.begin() as session:
        for model in (RotationState, InjuryStatus, Contract, RosterMembership, ImpactSnapshot):
            session.execute(delete(model).where(model.player_id.in_(PLAYERS)))
        session.execute(delete(Player).where(Player.nba_player_id.in_(PLAYERS)))
        session.execute(delete(Stint).where(Stint.game_id.in_(game_ids)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))
        session.execute(delete(LoadLog).where(LoadLog.game_date.in_([_day(g) for g in GAMES])))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """BOS's 22 games against NYK with box scores and stints, the last with a garbage-time stint;
    rosters, an injury report with an assignment, a contract snapshot, local BPM, and Win Shares
    and VORP for two starters; the view refreshed, rotation state written and every derive step
    recorded for the last date."""
    clear(db)
    report = datetime.combine(LAST, time(17), EASTERN)
    with db.begin() as session:
        session.execute(insert(Game), [_game(g) for g in GAMES])
        session.execute(insert(PlayerGame), [line for g in GAMES for line in _lines(g)])
        session.execute(insert(Stint), [row for g in GAMES for row in _stints(g)])
        session.execute(
            insert(RosterMembership),
            [
                *(_membership(p, BOS) for p in [*STARTERS, *BENCH, ASSIGNED]),
                _membership(TWO_WAY, BOS, "two_way"),
                *(_membership(p, NYK) for p in NYK_FIVE),
            ],
        )
        # Every BOS player is named but the assigned one, whose `player` row has yet to land.
        session.execute(
            insert(Player),
            [{"nba_player_id": p, "name": f"Player {p}"} for p in PLAYERS if p != ASSIGNED],
        )
        session.execute(
            insert(InjuryStatus),
            [
                _injury(ASSIGNED, BOS, report, "Out", "G League - On Assignment"),
                _injury(BENCH[2], BOS, report, "Questionable", "Injury/Illness - Left Knee"),
            ],
        )
        session.execute(
            insert(Contract),
            [
                _contract(BENCH[2], LAST + timedelta(days=20)),
                _contract(WAIVED, LAST + timedelta(days=20)),
                # Already guaranteed by the date.
                _contract(BENCH[1], LAST - timedelta(days=1)),
            ],
        )
        session.execute(
            insert(ImpactSnapshot),
            [
                {"date": FIRST, "player_id": p, "source": "bpm_local", "value": 1.0}
                for p in [*STARTERS, *BENCH]
            ]
            + [{"date": FIRST, "player_id": STARTERS[0], "source": "darko", "value": 3.5}]
            # Season-to-date value snapshots: the latest on or before the tier date is the board's.
            + [
                _value(_day(20), STARTERS[1], "ws_local", 2.0, 1.2, 0.8),
                _value(_day(21), STARTERS[1], "ws_local", 2.4, 1.5, 0.9),
                _value(_day(21), STARTERS[1], "vorp_local", 0.6),
                _value(_day(21), STARTERS[2], "ws_local", -0.3, 0.1, -0.4),
            ],
        )
        now = datetime.now(EASTERN)
        session.execute(
            insert(LoadLog),
            [
                {
                    "table_name": step.table,
                    "game_date": LAST,
                    "step": step.name,
                    "status": "succeeded",
                    "started_at": now,
                    "finished_at": now,
                }
                for step in WRITES
            ],
        )
    refresh_windows(db)
    compute_rotation_state(db, LAST, TierRule())
    yield db
    clear(db)
    refresh_windows(db)


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        yield client


def _row(stint: tuple[str, list[int], int, int, int, int]) -> StintRow:
    game_id, five, poss, opp_poss, scored, allowed = stint
    return StintRow(game_id, _key(five), poss, opp_poss, scored, allowed)


def test_groups_rank_by_offensive_possessions_with_recent_totals_beside() -> None:
    a, b = [1, 2, 3, 4, 5], [1, 2, 3, 4, 6]
    stints = [
        _row(("g1", a, 20, 20, 30, 20)),
        _row(("g2", a, 10, 12, 10, 12)),
        _row(("g2", b, 25, 25, 20, 30)),
    ]

    fives = top_groups(stints, 5, {"g2"})

    assert [group.players for group in fives] == [(1, 2, 3, 4, 5), (1, 2, 3, 4, 6)]
    first = fives[0]
    assert (first.season.possessions, first.season.opp_possessions) == (30, 32)
    assert first.season.net_rating == pytest.approx(100 * 40 / 30 - 100 * 32 / 32)
    assert first.recent.net_rating == pytest.approx(0.0)
    assert fives[1].season.net_rating == pytest.approx(-40.0)


def test_pairs_count_every_two_on_the_floor_and_keep_the_top_ten() -> None:
    pairs = top_groups([_row(("g1", [1, 2, 3, 4, 5], 10, 10, 10, 10))], 2, set())

    assert len(pairs) == 10
    assert pairs[0].players == (1, 2)
    assert pairs[0].recent.possessions == 0
    assert pairs[0].recent.net_rating is None


def test_a_group_with_no_defensive_possessions_has_no_net_rating() -> None:
    [group] = top_groups([_row(("g1", [1, 2, 3, 4, 5], 10, 0, 10, 0))], 5, set())

    assert group.season.net_rating is None


def test_every_rostered_player_has_a_tier_and_three_sparklines(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    response = client.get("/api/teams/BOS")

    assert response.status_code == 200
    page = response.json()
    assert page["date"] == LAST.isoformat()
    assert page["tier_date"] == LAST.isoformat()
    board = {row["player"]["player_id"]: row for row in page["tier_board"]}
    assert set(board) == set(BOS_ROSTER)
    for row in board.values():
        assert row["tier"] in {"injured", "starter", "core", "fringe", "out"}
        for series in ("minutes_share", "on_off_net", "impact"):
            assert len(row[series]) <= 20
    starter = board[STARTERS[1]]
    assert starter["tier"] == "starter"
    assert [p["game_date"] for p in starter["minutes_share"]] == [
        _day(g).isoformat() for g in GAMES[2:]
    ]
    assert starter["minutes_share"][-1]["value"] == pytest.approx(2400 / GAME_SECONDS)
    assert starter["impact_source"] == "bpm_local"
    assert len(starter["impact"]) == 20
    assert board[STARTERS[0]]["impact_source"] == "darko"
    assert board[ASSIGNED]["badges"] == ["g_league"]
    assert board[ASSIGNED]["player"]["name"] is None
    assert board[TWO_WAY]["badges"] == ["two_way"]
    assert board[TWO_WAY]["impact"] == []
    tiers = [row["tier"] for row in page["tier_board"]]
    assert tiers == sorted(tiers, key=["injured", "starter", "core", "fringe", "out"].index)


def test_the_tier_board_carries_season_win_shares_and_vorp_on_the_tier_date(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = client.get("/api/teams/BOS").json()

    board = {row["player"]["player_id"]: row for row in page["tier_board"]}
    assert board[STARTERS[1]]["ws"] == pytest.approx(2.4)
    assert board[STARTERS[1]]["vorp"] == pytest.approx(0.6)
    assert board[STARTERS[2]]["ws"] == pytest.approx(-0.3)
    assert board[STARTERS[2]]["vorp"] is None
    assert board[TWO_WAY]["ws"] is None
    assert board[TWO_WAY]["vorp"] is None


def test_closing_lineups_are_the_last_five_close_games_final_stints(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = client.get("/api/teams/bos", params={"date": LAST.isoformat()}).json()

    closing = page["closing_lineups"]
    assert [c["game_id"] for c in closing] == [_game_id(g) for g in (21, 19, 17, 15, 13)]
    assert closing[0]["opponent"] == "NYK"
    assert closing[0]["margin"] == 5
    assert [p["player_id"] for p in closing[0]["players"]] == sorted(CLOSERS)


def test_lineups_leave_garbage_time_out_unless_included(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    excluded = client.get("/api/teams/BOS").json()
    included = client.get("/api/teams/BOS", params={"garbage_time": "include"}).json()

    def starters(page: dict[str, Any]) -> dict[str, Any]:
        [five] = [
            lineup
            for lineup in page["lineups"]
            if [p["player_id"] for p in lineup["players"]] == STARTERS
        ]
        return five

    assert excluded["garbage_time"] == "exclude"
    # 30-20 on 25 a side in every game.
    assert starters(excluded)["possessions"] == 22 * 25
    assert starters(excluded)["net_rating"] == pytest.approx(40.0)
    assert starters(excluded)["possessions_10"] == 10 * 25
    # Plus 0-20 on 10 a side.
    assert starters(included)["possessions"] == 22 * 25 + 10
    assert starters(included)["net_rating"] == pytest.approx(100 * 660 / 560 - 100 * 460 / 560)
    assert starters(included)["net_rating_10"] != starters(excluded)["net_rating_10"]
    assert included["combos"] != excluded["combos"]
    assert len(excluded["lineups"]) == 2
    assert len(excluded["combos"]) == 10


def test_the_availability_board_is_the_latest_report_and_assignments(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    board = client.get("/api/teams/BOS").json()["availability"]

    assert board["source"] == "pdf"
    assert board["stale"] is False
    assert board["game_date"] == LAST.isoformat()
    assert [
        (row["player"]["player_id"], row["status"], row["g_league"]) for row in board["players"]
    ] == [(ASSIGNED, "Out", True), (BENCH[2], "Questionable", False)]


def test_a_report_covering_a_back_to_back_lists_the_nearer_game_only(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    report = datetime.combine(LAST, time(17), EASTERN)
    tomorrow = {**_injury(BENCH[2], BOS, report, "Out", "Rest"), "game_date": LAST + timedelta(1)}
    with seeded.begin() as session:
        session.execute(insert(InjuryStatus), tomorrow)

    board = client.get("/api/teams/BOS").json()["availability"]

    assert board["game_date"] == LAST.isoformat()
    assert [(row["player"]["player_id"], row["status"]) for row in board["players"]] == [
        (ASSIGNED, "Out"),
        (BENCH[2], "Questionable"),
    ]


def test_a_date_before_any_report_has_an_empty_board(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = client.get("/api/teams/BOS", params={"date": _day(3).isoformat()}).json()

    assert page["tier_date"] == _day(3).isoformat()
    # No rotation state was written for that date.
    assert page["tier_board"] == []
    assert page["availability"] == {
        "report_datetime": None,
        "game_date": None,
        "source": None,
        "stale": False,
        "players": [],
    }


def test_roster_counts_and_contract_notes(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = client.get("/api/teams/BOS").json()

    assert page["roster"] == {
        "standard": 9,
        "standard_limit": 15,
        "two_way": 1,
        "two_way_limit": 3,
    }
    [note] = page["contracts"]
    assert note["player"]["player_id"] == BENCH[2]
    assert note["days_until_guarantee"] == 20
    assert note["guaranteed_after"] is None


def test_an_unknown_team_is_404(seeded: sessionmaker[Session], client: TestClient) -> None:
    assert client.get("/api/teams/XYZ").status_code == 404


def test_the_team_route_answers_within_two_seconds(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    started = clock.perf_counter()
    response = client.get("/api/teams/BOS")
    elapsed = clock.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 2.0


def test_teams_lists_all_30_with_records(seeded: sessionmaker[Session], client: TestClient) -> None:
    response = client.get("/api/teams")

    assert response.status_code == 200
    body = response.json()
    assert body["season"] == SEASON
    assert len(body["teams"]) == 30
    teams = {team["tricode"]: team for team in body["teams"]}
    assert (teams["BOS"]["wins"], teams["BOS"]["losses"]) == (22, 0)
    assert (teams["NYK"]["wins"], teams["NYK"]["losses"]) == (0, 22)
    assert teams["BOS"]["latest_derived_date"] == LAST.isoformat()
    assert teams["BOS"]["conference"] == "East"

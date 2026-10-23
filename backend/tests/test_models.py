from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import (
    Contract,
    Game,
    ImpactSnapshot,
    InjuryStatus,
    PlayerGame,
    Transaction,
)

# Never a real game ID, so the rows these tests add cannot collide with loaded data. Each test's
# session closes without committing, so nothing they add persists.
GAME_ID = "0029999999"
BOS = 1610612738
NYK = 1610612752
JAYSON_TATUM = 1628369
# `player_game`'s NOT NULL counts, all zero for a DNP.
COUNTS = [
    "seconds",
    "pts",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
    "plus_minus",
]


def _game(phase: str = "regular") -> Game:
    return Game(
        game_id=GAME_ID,
        season="2025-26",
        game_date=date(2025, 10, 21),
        phase=phase,
        home_team_id=BOS,
        away_team_id=NYK,
        status="final",
        home_score=110,
        away_score=104,
        home_b2b=False,
        away_b2b=False,
    )


def _dnp(dnp_reason: str | None) -> PlayerGame:
    return PlayerGame(
        game_id=GAME_ID,
        player_id=JAYSON_TATUM,
        team_id=BOS,
        is_starter=False,
        dnp_reason=dnp_reason,
        source="stats",
        **dict.fromkeys(COUNTS, 0),
    )


def test_a_game_and_a_dnp_with_listed_values_load(db: sessionmaker[Session]) -> None:
    with db() as session:
        session.add(_game("cup_knockout"))
        session.flush()
        session.add(_dnp("injury"))
        session.flush()


def test_game_rejects_an_unlisted_phase(db: sessionmaker[Session]) -> None:
    with db() as session:
        session.add(_game("preseason"))
        with pytest.raises(IntegrityError, match="ck_game_phase"):
            session.flush()


def test_player_game_rejects_an_unlisted_dnp_reason(db: sessionmaker[Session]) -> None:
    with db() as session:
        session.add(_game())
        session.flush()
        session.add(_dnp("rest"))
        with pytest.raises(IntegrityError, match="ck_player_game_dnp_reason"):
            session.flush()


def _injury(status: str) -> InjuryStatus:
    return InjuryStatus(
        report_datetime=datetime(2025, 10, 21, 21, 30, tzinfo=UTC),
        game_date=date(2025, 10, 21),
        player_id=JAYSON_TATUM,
        team_id=BOS,
        status=status,
        reason="Injury/Illness - Right Achilles; Repair",
        source="pdf",
    )


def _impact(source: str) -> ImpactSnapshot:
    return ImpactSnapshot(date=date(2025, 10, 21), player_id=JAYSON_TATUM, source=source, value=3.1)


def test_a_snapshot_of_each_kind_with_listed_values_loads(db: sessionmaker[Session]) -> None:
    with db() as session:
        session.add_all(
            [
                _injury("Out"),
                _impact("bpm_local"),
                Transaction(
                    date=date(2025, 7, 6),
                    type="assign",
                    player_id=JAYSON_TATUM,
                    from_team_id=BOS,
                    detail="Assigned to Maine Celtics",
                ),
                Contract(
                    player_id=JAYSON_TATUM,
                    team_id=BOS,
                    season="2025-26",
                    salary=54_126_450,
                    guaranteed=54_126_450,
                    option_type="player",
                    two_way=False,
                    as_of_date=date(2025, 10, 20),
                    source="spotrac",
                ),
            ]
        )
        session.flush()


def test_injury_status_rejects_an_unlisted_status(db: sessionmaker[Session]) -> None:
    with db() as session:
        # ESPN's own status, which the overlay maps to Questionable (spec §4.3).
        session.add(_injury("Day-To-Day"))
        with pytest.raises(IntegrityError, match="ck_injury_status_status"):
            session.flush()


def test_impact_snapshot_rejects_an_unlisted_source(db: sessionmaker[Session]) -> None:
    with db() as session:
        session.add(_impact("raptor"))
        with pytest.raises(IntegrityError, match="ck_impact_snapshot_source"):
            session.flush()

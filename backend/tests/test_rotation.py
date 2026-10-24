from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import (
    Game,
    InjuryStatus,
    PlayerGame,
    RosterMembership,
    RotationState,
)
from closinglineup.derive.availability import Availability
from closinglineup.derive.roster import ON_ASSIGNMENT
from closinglineup.derive.rotation import TierInputs, TierRule, compute_rotation_state, tier
from closinglineup.derive.windows import refresh_windows
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN

RULE = TierRule()


def _inputs(
    *,
    availability: Availability = "active",
    games: int = 10,
    played_10: int = 10,
    share: float | None = 0.0,
    played_5: int = 5,
    started_5: int = 0,
) -> TierInputs:
    return TierInputs(availability, games, played_10, share, played_5, started_5)


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (_inputs(availability="out", started_5=5, share=0.15), "injured"),
        (_inputs(availability="doubtful"), "injured"),
        (_inputs(started_5=4), "starter"),
        (_inputs(share=0.036), "core"),
        (_inputs(played_10=5), "fringe"),
        (_inputs(played_10=4, played_5=4), "out"),
        # Precedence: an injured starter is Injured; a starter with a Core share is a Starter.
        (_inputs(availability="out", started_5=4, share=0.1), "injured"),
        (_inputs(started_5=5, share=0.15), "starter"),
        # A questionable or G League-assigned player is tiered by his games.
        (_inputs(availability="questionable", started_5=4), "starter"),
        (_inputs(availability="g_league", played_10=6, share=0.01), "fringe"),
        (_inputs(availability="g_league", played_10=2, played_5=2, share=0.01), "out"),
        # 3 of 5 started falls short of 4 of 5; a short window computes on what it has.
        (_inputs(started_5=3), "fringe"),
        (_inputs(games=3, played_10=3, played_5=3, started_5=3), "starter"),
        (_inputs(games=3, played_10=3, played_5=4, started_5=3), "fringe"),
        (_inputs(games=0, played_10=0, played_5=0, share=None), "out"),
    ],
)
def test_tier_is_the_first_rule_the_inputs_meet(inputs: TierInputs, expected: str) -> None:
    assert tier(inputs, RULE) == expected


def test_the_rule_reads_its_thresholds_from_settings() -> None:
    settings = Settings(
        database_url="postgresql://unused",
        tier_starter_min=(3, 4),
        tier_core_share=0.1,
        tier_fringe_played_rate=0.3,
    )

    assert TierRule.from_settings(settings) == TierRule(3, 4, 0.1, 0.3)


# SAC's game g, 1 to 10, is on DAY + g - 1 against POR, in a season no real data has.
SEASON = "2019-20"
FIRST = date(2019, 11, 1)
DAY = FIRST + timedelta(days=9)
SAC = 1610612758
POR = 1610612757
GAMES = range(1, 11)
GAME_SECONDS = 14400

# Each named SAC player's minutes in game g (0 for a DNP line, None for no line), and whether he
# started it.
STARTER = 960001  # 30 minutes a game, starting 6 to 9: 4 of his last 5.
INJURED = 960002  # Starts 1 to 9, then out injured on the day's report.
TWO_WAY = 960003  # 20 off the bench on a two-way contract: Core.
ASSIGNED = 960004  # 3 minutes in 1 to 6, then on G League assignment: Fringe.
OUT = 960005  # 2 minutes in 1 and 2, a coach's decision in 10.
SHORT = 960006  # Joins before game 8 and starts 8 to 10.
LATE = 960007  # Core; reports after the day list him out.
CLEARED = 960008  # Core; out on the day's 10:00 report, off its 17:15 one.
SUSPENDED = 960009  # Core; suspended for game 10.
MISSING = 960010  # Core; no line in game 10's box.
SAC_BENCH = 960011  # The rest of SAC's minutes, starting every game.
POR_BENCH = 960012
NAMED = [STARTER, INJURED, TWO_WAY, ASSIGNED, OUT, SHORT, LATE, CLEARED, SUSPENDED, MISSING]
PLAYERS = [*NAMED, SAC_BENCH, POR_BENCH]
STALE = 960099


def _day(g: int) -> date:
    return FIRST + timedelta(days=g - 1)


def _game_id(g: int) -> str:
    return f"00219009{g:02d}"


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN)


def _minutes(player_id: int, g: int) -> tuple[int | None, bool, str | None]:
    """(minutes, started, DNP reason) of a named SAC player in game g; None minutes for no line."""
    last = g == 10
    if player_id == STARTER:
        return 30, 6 <= g <= 9, None
    if player_id == INJURED:
        return (0, False, "injury") if last else (30, True, None)
    if player_id == ASSIGNED:
        return (3, False, None) if g <= 6 else (None, False, None)
    if player_id == OUT:
        return (2, False, None) if g <= 2 else (0, False, "cd") if last else (None, False, None)
    if player_id == SHORT:
        return (25, True, None) if g >= 8 else (None, False, None)
    if player_id == SUSPENDED:
        return (0, False, "suspension") if last else (20, False, None)
    if player_id == MISSING:
        return (None, False, None) if last else (20, False, None)
    return 20, False, None


def _line(
    game_id: str,
    player_id: int,
    team_id: int,
    seconds: int,
    *,
    starter: bool = False,
    dnp_reason: str | None = None,
) -> dict[str, Any]:
    counts = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl")
    return {
        "game_id": game_id,
        "player_id": player_id,
        "team_id": team_id,
        "is_starter": starter,
        "seconds": seconds,
        "dnp_reason": dnp_reason,
        **dict.fromkeys((*counts, "blk", "tov", "pf", "plus_minus"), 0),
        "source": "stats",
    }


def _lines(g: int) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    used = 0
    for player_id in NAMED:
        minutes, started, dnp = _minutes(player_id, g)
        if minutes is None:
            continue
        used += minutes * 60
        lines.append(
            _line(_game_id(g), player_id, SAC, minutes * 60, starter=started, dnp_reason=dnp)
        )
    lines.append(_line(_game_id(g), SAC_BENCH, SAC, GAME_SECONDS - used, starter=True))
    lines.append(_line(_game_id(g), POR_BENCH, POR, GAME_SECONDS, starter=True))
    return lines


def _membership(
    player_id: int, team_id: int, start: date, contract: str = "standard"
) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "start_date": start,
        "end_date": None,
        "contract_type": contract,
        "source": "player_game",
    }


def _report(at: datetime, player_id: int, reason: str, game_date: date = DAY) -> dict[str, Any]:
    return {
        "report_datetime": at,
        "game_date": game_date,
        "player_id": player_id,
        "team_id": SAC,
        "status": "Out",
        "reason": reason,
        "source": "pdf",
    }


ANKLE = "Injury/Illness - Left Ankle; Sprain"


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(RotationState).where(RotationState.player_id.in_([*PLAYERS, STALE])))
        session.execute(delete(InjuryStatus).where(InjuryStatus.player_id.in_(PLAYERS)))
        session.execute(
            delete(PlayerGame).where(PlayerGame.game_id.in_([_game_id(g) for g in GAMES]))
        )
        session.execute(delete(RosterMembership).where(RosterMembership.player_id.in_(PLAYERS)))
        session.execute(delete(Game).where(Game.game_id.in_([_game_id(g) for g in GAMES])))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """SAC's and POR's ten games, their players' roster spans and the day's injury reports, and
    the view refreshed over them."""
    _clear(db)
    start = FIRST - timedelta(days=30)
    with db.begin() as session:
        session.execute(
            insert(Game),
            [
                {
                    "game_id": _game_id(g),
                    "season": SEASON,
                    "game_date": _day(g),
                    "phase": "regular",
                    "home_team_id": SAC,
                    "away_team_id": POR,
                    "status": "final",
                    "home_b2b": False,
                    "away_b2b": False,
                }
                for g in GAMES
            ],
        )
        session.execute(insert(PlayerGame), [line for g in GAMES for line in _lines(g)])
        session.execute(
            insert(RosterMembership),
            [
                *(
                    _membership(player_id, SAC, start)
                    for player_id in [*NAMED, SAC_BENCH]
                    if player_id not in (TWO_WAY, SHORT)
                ),
                _membership(TWO_WAY, SAC, start, "two_way"),
                _membership(SHORT, SAC, _day(8)),
                _membership(POR_BENCH, POR, start),
            ],
        )
        session.execute(
            insert(InjuryStatus),
            [
                _report(_at(DAY, 10), INJURED, ANKLE),
                _report(_at(DAY, 10), CLEARED, ANKLE),
                _report(_at(DAY, 17, 15), INJURED, ANKLE),
                _report(_at(DAY, 17, 15), ASSIGNED, ON_ASSIGNMENT),
                # Reported after the day: for its game, and for the next one.
                _report(_at(DAY + timedelta(days=1), 0, 30), LATE, ANKLE),
                _report(_at(DAY + timedelta(days=1), 10), LATE, ANKLE, DAY + timedelta(days=1)),
            ],
        )
    refresh_windows(db)
    yield db
    _clear(db)
    refresh_windows(db)


def _states(db: sessionmaker[Session]) -> dict[int, RotationState]:
    with db() as session:
        found = session.scalars(select(RotationState).where(RotationState.game_date == DAY))
        return {state.player_id: state for state in found}


def test_each_player_gets_the_tier_badges_and_availability_the_rule_gives(
    seeded: sessionmaker[Session],
) -> None:
    result = compute_rotation_state(seeded, DAY, RULE)

    states = _states(seeded)
    assert result == {"players": len(PLAYERS), "changed": len(PLAYERS), "removed": 0}
    assert {
        player_id: (state.tier, state.availability, state.badges, state.is_short)
        for player_id, state in states.items()
        if player_id in NAMED
    } == {
        STARTER: ("starter", "active", [], False),
        INJURED: ("injured", "out", [], False),
        TWO_WAY: ("core", "active", ["two_way"], False),
        ASSIGNED: ("fringe", "g_league", ["g_league"], False),
        OUT: ("out", "active", [], False),
        SHORT: ("starter", "active", [], True),
        LATE: ("core", "active", [], False),
        CLEARED: ("core", "active", [], False),
        SUSPENDED: ("core", "suspended", [], False),
        MISSING: ("core", "inactive", [], False),
    }
    assert states[POR_BENCH].team_id == POR


def test_an_injured_player_is_injured_though_his_share_is_cores_and_his_starts_a_starters(
    seeded: sessionmaker[Session],
) -> None:
    compute_rotation_state(seeded, DAY, RULE)

    injured = _states(seeded)[INJURED]
    assert injured.tier == "injured"
    assert injured.minutes_share_10 == pytest.approx(270 * 60 / (10 * GAME_SECONDS))
    assert injured.starter_rate_5 == 1.0


def test_inputs_hold_every_number_the_rule_read(seeded: sessionmaker[Session]) -> None:
    compute_rotation_state(seeded, DAY, RULE)

    states = _states(seeded)
    starter = states[STARTER]
    assert starter.inputs == {
        "availability": "active",
        "games_in_window": 10,
        "games_played_10": 10,
        "minutes_share_10": pytest.approx(300 * 60 / (10 * GAME_SECONDS)),
        "played_rate_10": 1.0,
        "played_5": 5,
        "started_5": 4,
        "starter_rate_5": 0.8,
    }
    assert (starter.minutes_share_10, starter.starter_rate_5, starter.played_rate_10) == (
        starter.inputs["minutes_share_10"],
        0.8,
        1.0,
    )
    short = states[SHORT].inputs
    assert (short["games_in_window"], short["played_5"], short["started_5"]) == (3, 3, 3)
    assert states[ASSIGNED].played_rate_10 == 0.6


def test_a_rerun_leaves_identical_rows_and_drops_a_player_team_no_longer_there(
    seeded: sessionmaker[Session],
) -> None:
    compute_rotation_state(seeded, DAY, RULE)
    with seeded() as session:
        first = [
            (state.player_id, state.tier, state.inputs, state.badges, state.updated_at)
            for state in session.scalars(
                select(RotationState)
                .where(RotationState.game_date == DAY)
                .order_by(RotationState.player_id)
            )
        ]
    with seeded.begin() as session:
        session.execute(
            insert(RotationState),
            {
                "player_id": STALE,
                "team_id": SAC,
                "game_date": DAY,
                "tier": "out",
                "availability": "active",
                "badges": [],
                "is_short": False,
                "inputs": {},
            },
        )

    again = compute_rotation_state(seeded, DAY, RULE)

    with seeded() as session:
        second = [
            (state.player_id, state.tier, state.inputs, state.badges, state.updated_at)
            for state in session.scalars(
                select(RotationState)
                .where(RotationState.game_date == DAY)
                .order_by(RotationState.player_id)
            )
        ]
    assert again == {"players": len(PLAYERS), "changed": 0, "removed": 1}
    assert second == first


def test_a_date_no_team_played_has_no_rows(seeded: sessionmaker[Session]) -> None:
    result = compute_rotation_state(seeded, FIRST - timedelta(days=1), RULE)

    assert result == {"players": 0, "changed": 0, "removed": 0}

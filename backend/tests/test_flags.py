from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import (
    Contract,
    Game,
    InjuryStatus,
    PlayerFlag,
    PlayerGame,
    RosterMembership,
    RotationEvent,
    RotationState,
    Stint,
)
from closinglineup.derive.flags import (
    FlagInputs,
    FlagRule,
    Guarantee,
    TierChange,
    bleeding,
    compute_flags,
    contract_pressure,
    earning_trust,
    flags,
    minutes_cliff,
    usage_up_efficiency_down,
    yo_yo,
)
from closinglineup.derive.rotation import TierRule, compute_rotation_state
from closinglineup.derive.windows import refresh_windows
from closinglineup.sources.injury_pdf import EASTERN

RULE = FlagRule()
TODAY = date(2026, 1, 1)


@pytest.mark.parametrize(
    ("share_5", "flagged"),
    [(0.0599, True), (0.06, False), (0.07, False)],
)
def test_a_minutes_cliff_is_a_5_game_share_below_60_percent_of_the_20_game(
    share_5: float, flagged: bool
) -> None:
    found = minutes_cliff(FlagInputs(tier="core", share_5=share_5, share_20=0.1), RULE)

    assert (found is not None) == flagged
    if found is not None:
        assert found == {"share_5": 0.0599, "share_20": 0.1, "ratio": 0.599}


def test_an_injured_player_never_has_a_minutes_cliff() -> None:
    assert minutes_cliff(FlagInputs(tier="injured", share_5=0.0, share_20=0.1), RULE) is None
    assert minutes_cliff(FlagInputs(tier="out", share_5=0.0, share_20=0.1), RULE) is not None


@pytest.mark.parametrize(
    ("usage", "ts", "flagged"),
    [
        (0.03, -0.04, True),
        # Exactly 3 and 4 points, however the subtraction rounds.
        (0.36 - 0.33, 0.27 - 0.31, True),
        (0.0299, -0.04, False),
        (0.03, -0.0399, False),
        (0.05, None, False),
    ],
)
def test_usage_up_efficiency_down_is_usage_up_3_and_ts_down_4_points_10_games_vs_season(
    usage: float, ts: float | None, flagged: bool
) -> None:
    inputs = FlagInputs(usage_10=0.3, usage_delta=usage, ts_10=0.5, ts_delta=ts)

    assert (usage_up_efficiency_down(inputs, RULE) is not None) == flagged


@pytest.mark.parametrize(
    ("net", "possessions", "on_off", "flagged"),
    [
        # Off-court net is the on-court net less on/off: here +1.
        (-5.0, 200, -6.0, True),
        (-5.0, 199, -6.0, False),
        (-4.9, 400, -6.0, False),
        (-8.0, 400, -8.0, False),
        (-8.0, None, -9.0, False),
    ],
)
def test_bleeding_is_a_bad_on_court_net_on_200_possessions_while_the_team_wins_without_him(
    net: float, possessions: int | None, on_off: float, flagged: bool
) -> None:
    inputs = FlagInputs(net_10=net, possessions_10=possessions, on_off_10=on_off)

    found = bleeding(inputs, RULE)

    assert (found is not None) == flagged
    if found is not None:
        assert found == {"on_court_net": -5.0, "possessions": 200, "off_court_net": 1.0}


@pytest.mark.parametrize(
    ("shares", "flagged"),
    [
        ((0.05, 0.06, 0.07), True),
        ((0.05, 0.06, 0.06), False),
        ((0.07, 0.06, 0.08), False),
        ((None, 0.06, 0.07), False),
        ((0.06, 0.07), False),
    ],
)
def test_earning_trust_is_a_share_higher_in_each_of_three_5_game_windows(
    shares: tuple[float | None, ...], flagged: bool
) -> None:
    assert (earning_trust(FlagInputs(trust_shares=shares), RULE) is not None) == flagged


def test_a_yo_yo_is_three_tier_changes() -> None:
    changes = (
        TierChange(date(2025, 12, 1), "core", "fringe"),
        TierChange(date(2025, 12, 9), "fringe", "core"),
        TierChange(date(2025, 12, 20), "core", "fringe"),
    )

    assert yo_yo(FlagInputs(changes=changes[:2]), RULE) is None
    assert yo_yo(FlagInputs(changes=changes), RULE) == {
        "events": 3,
        "changes": [
            {"game_date": "2025-12-01", "from_tier": "core", "to_tier": "fringe"},
            {"game_date": "2025-12-09", "from_tier": "fringe", "to_tier": "core"},
            {"game_date": "2025-12-20", "from_tier": "core", "to_tier": "fringe"},
        ],
    }


@pytest.mark.parametrize(
    ("days", "guaranteed_after", "flagged"),
    [
        (30, 1_000_000, True),
        (31, 1_000_000, False),
        (0, 1_000_000, True),
        (-1, 1_000_000, False),
        # The minimum deal no line prices.
        (10, None, True),
        # Nothing more becomes guaranteed.
        (10, 250_000, False),
    ],
)
def test_contract_pressure_is_a_guarantee_rising_within_30_days(
    days: int, guaranteed_after: int | None, flagged: bool
) -> None:
    guarantee = Guarantee(TODAY + timedelta(days=days), 250_000, guaranteed_after)

    found = contract_pressure(FlagInputs(guarantee=guarantee), RULE, TODAY)

    assert (found is not None) == flagged
    if found is not None:
        assert found == {
            "reason": "guarantee",
            "guarantee_date": (TODAY + timedelta(days=days)).isoformat(),
            "days": days,
            "guaranteed": 250_000,
            "guaranteed_after": guaranteed_after,
        }


def test_contract_pressure_is_a_two_way_within_5_games_of_the_limit() -> None:
    assert contract_pressure(FlagInputs(two_way_active=44), RULE, TODAY) is None
    assert contract_pressure(FlagInputs(two_way_active=45), RULE, TODAY) == {
        "reason": "two_way",
        "active_games": 45,
        "limit": 50,
        "remaining": 5,
    }


def test_flags_keep_spec_order_and_only_the_raised() -> None:
    inputs = FlagInputs(
        tier="fringe",
        share_5=0.01,
        share_20=0.05,
        two_way_active=48,
    )

    assert list(flags(inputs, RULE, TODAY)) == ["minutes_cliff", "contract_pressure"]


# SAC's game g, 1 to 20, is on _day(g) against POR, in a season no other test uses.
SEASON = "2015-16"
FIRST = date(2015, 11, 1)
SAC = 1610612758
POR = 1610612757
GAMES = range(1, 21)
DAY = FIRST + timedelta(days=19)
GAME_SECONDS = 14400

CLIFF = 980001  # 20 minutes off the bench in games 1 to 15, then 5.
HURT = 980002  # As CLIFF, but out injured from game 16: Injured, so no cliff.
TRUST = 980003  # 10 minutes in games 1 to 10, 15 in 11 to 15, 20 in 16 to 20.
GUARANTEE = 980004  # A minimum deal guaranteed 30 days after DAY.
LATE = 980005  # A guarantee 31 days after DAY; an older snapshot's, 5 days after, is stale.
SHOOTER = 980006  # 5 of SAC's 50 attempts for 10 points in games 1 to 10, then 10 for 10.
TWO_WAY = 980007  # 10 minutes a game on a two-way; inactive in game 1, DNP-CD in game 2.
BLEED = 980008  # On the floor for SAC's bad first quarters in games 11 to 20.
YO_YO = 980009  # An arrival, then three tier changes on full windows in the last 20 games.
TWICE = 980010  # Two tier changes on full windows in them, and one before them.
ARRIVED = 980013  # An arrival on a full window, and two tier changes after it.
SHORT = 980014  # A tier change while his 10-game window was short, and two after it was full.
SAC_BENCH = 980011  # The rest of SAC's minutes and attempts.
POR_BENCH = 980012
NAMED = [CLIFF, HURT, TRUST, GUARANTEE, LATE, SHOOTER, TWO_WAY, BLEED, YO_YO, TWICE, ARRIVED, SHORT]
PLAYERS = [*NAMED, SAC_BENCH, POR_BENCH]
SAC_FILLERS = [980031, 980032, 980033, 980034]
POR_FIVE = "-".join(str(player_id) for player_id in range(980041, 980046))


def _day(g: int) -> date:
    return FIRST + timedelta(days=g - 1)


def _game_id(g: int) -> str:
    return f"00215009{g:02d}"


def _seconds(player_id: int, g: int) -> tuple[int, str | None]:
    """(seconds, DNP reason) of a named SAC player in game g."""
    if player_id == CLIFF:
        return (1200 if g <= 15 else 300), None
    if player_id == HURT:
        return (1200, None) if g <= 15 else (0, "injury")
    if player_id == TRUST:
        return (600 if g <= 10 else 900 if g <= 15 else 1200), None
    if player_id in (SHOOTER, BLEED):
        return 1800, None
    if player_id == TWO_WAY:
        return {1: (0, "inactive"), 2: (0, "cd")}.get(g, (600, None))
    return 1200, None


def _line(
    g: int,
    player_id: int,
    team_id: int,
    seconds: int,
    *,
    starter: bool = False,
    dnp_reason: str | None = None,
    fga: int = 0,
    pts: int = 0,
) -> dict[str, Any]:
    counts = ("fgm", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk", "tov")
    return {
        "game_id": _game_id(g),
        "player_id": player_id,
        "team_id": team_id,
        "is_starter": starter,
        "seconds": seconds,
        "dnp_reason": dnp_reason,
        "fga": fga,
        "pts": pts,
        **dict.fromkeys((*counts, "pf", "plus_minus"), 0),
        "source": "stats",
    }


def _lines(g: int) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    shooter_fga = 5 if g <= 10 else 10
    for player_id in NAMED:
        seconds, dnp = _seconds(player_id, g)
        shooter = player_id == SHOOTER
        lines.append(
            _line(
                g,
                player_id,
                SAC,
                seconds,
                starter=player_id in (SHOOTER, BLEED),
                dnp_reason=dnp,
                fga=shooter_fga if shooter else 0,
                pts=10 if shooter else 0,
            )
        )
    used = sum(line["seconds"] for line in lines)
    lines.append(_line(g, SAC_BENCH, SAC, GAME_SECONDS - used, starter=True, fga=50 - shooter_fga))
    lines.append(_line(g, POR_BENCH, POR, GAME_SECONDS, starter=True))
    return lines


def _stints(g: int) -> list[dict[str, Any]]:
    """Games 11 to 20: SAC loses the first quarter 20-28 on 25 possessions a side with BLEED on
    the floor, and wins the second 30-20 without him."""
    if g <= 10:
        return []
    with_him = "-".join(str(p) for p in sorted([BLEED, SAC_BENCH, *SAC_FILLERS[:3]]))
    without = "-".join(str(p) for p in sorted([SAC_BENCH, *SAC_FILLERS]))
    rows: list[dict[str, Any]] = []
    for period, five, scored, allowed in ((1, with_him, 20, 28), (2, without, 30, 20)):
        for team_id, key, opp_key, pts_for, pts_against in (
            (SAC, five, POR_FIVE, scored, allowed),
            (POR, POR_FIVE, five, allowed, scored),
        ):
            rows.append(
                {
                    "game_id": _game_id(g),
                    "period": period,
                    "start_sec": 0.0,
                    "end_sec": 720.0,
                    "team_id": team_id,
                    "lineup_key": key,
                    "opp_lineup_key": opp_key,
                    "possessions": 25,
                    "pts_for": pts_for,
                    "pts_against": pts_against,
                    "is_garbage_time": False,
                }
            )
    return rows


def _membership(player_id: int, team_id: int, contract_type: str = "standard") -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "start_date": FIRST - timedelta(days=30),
        "end_date": None,
        "contract_type": contract_type,
        "source": "player_game",
    }


def _contract(
    player_id: int, as_of: date, guarantee_in: int, guaranteed_after: int | None
) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": SAC,
        "season": SEASON,
        "salary": None,
        "minimum_salary": True,
        "guaranteed": 0,
        "guarantee_date": DAY + timedelta(days=guarantee_in),
        "guaranteed_after": guaranteed_after,
        "as_of_date": as_of,
        "source": "hoopsrumors",
    }


def _event(player_id: int, day: date, from_tier: str | None, to_tier: str) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": SAC,
        "game_date": day,
        "prev_game_date": None,
        "from_tier": from_tier,
        "to_tier": to_tier,
        "signals": [],
    }


def _clear(db: sessionmaker[Session]) -> None:
    game_ids = [_game_id(g) for g in GAMES]
    with db.begin() as session:
        for model in (
            PlayerFlag,
            RotationEvent,
            RotationState,
            InjuryStatus,
            Contract,
            RosterMembership,
        ):
            session.execute(delete(model).where(model.player_id.in_(PLAYERS)))
        session.execute(delete(Stint).where(Stint.game_id.in_(game_ids)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """SAC's and POR's 20 games with their box scores and stints, roster spans, HURT's injury
    report, two contract snapshots and the events, the view refreshed over them, and rotation
    state written for every game date, each row short through game 9."""
    _clear(db)
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
        session.execute(insert(Stint), [stint for g in GAMES for stint in _stints(g)])
        session.execute(
            insert(RosterMembership),
            [
                *(
                    _membership(player_id, SAC, "two_way" if player_id == TWO_WAY else "standard")
                    for player_id in [*NAMED, SAC_BENCH]
                ),
                _membership(POR_BENCH, POR),
            ],
        )
        session.execute(
            insert(InjuryStatus),
            {
                "report_datetime": datetime.combine(DAY, time(10), EASTERN),
                "game_date": DAY,
                "player_id": HURT,
                "team_id": SAC,
                "status": "Out",
                "reason": "Injury/Illness - Left Ankle; Sprain",
                "source": "pdf",
            },
        )
        session.execute(
            insert(Contract),
            [
                _contract(LATE, DAY - timedelta(days=10), 5, 600_000),
                _contract(GUARANTEE, DAY - timedelta(days=3), 30, None),
                # Settled before DAY: the snapshot's upcoming guarantee is the one read.
                {**_contract(GUARANTEE, DAY - timedelta(days=3), -5, 400_000), "season": "2014-15"},
                _contract(LATE, DAY - timedelta(days=3), 31, 600_000),
            ],
        )
        session.execute(
            insert(RotationEvent),
            [
                _event(YO_YO, _day(1), None, "fringe"),
                _event(YO_YO, _day(10), "fringe", "core"),
                _event(YO_YO, _day(14), "core", "fringe"),
                _event(YO_YO, _day(20), "fringe", "core"),
                _event(TWICE, FIRST - timedelta(days=5), "fringe", "core"),
                _event(TWICE, _day(12), "core", "fringe"),
                _event(TWICE, _day(16), "fringe", "core"),
                _event(ARRIVED, _day(11), None, "core"),
                _event(ARRIVED, _day(14), "core", "fringe"),
                _event(ARRIVED, _day(18), "fringe", "core"),
                _event(SHORT, _day(5), "core", "fringe"),
                _event(SHORT, _day(14), "fringe", "core"),
                _event(SHORT, _day(18), "core", "fringe"),
            ],
        )
    refresh_windows(db)
    for g in GAMES:
        compute_rotation_state(db, _day(g), TierRule())
    yield db
    _clear(db)
    refresh_windows(db)


def _flags(db: sessionmaker[Session], day: date = DAY) -> dict[int, dict[str, dict[str, Any]]]:
    found: dict[int, dict[str, dict[str, Any]]] = {p: {} for p in [*NAMED, SAC_BENCH]}
    with db() as session:
        for row in session.scalars(select(PlayerFlag).where(PlayerFlag.game_date == day)):
            if row.player_id in PLAYERS:
                found.setdefault(row.player_id, {})[row.flag] = row.detail
    return found


def test_each_flag_is_raised_on_its_player_and_no_other(seeded: sessionmaker[Session]) -> None:
    compute_flags(seeded, DAY, FlagRule(two_way_limit=24))

    found = {player_id: set(raised) for player_id, raised in _flags(seeded).items()}
    # HURT's 5-game share is 0, a cliff were he not Injured.
    with seeded() as session:
        hurt = session.scalars(
            select(RotationState.tier).where(
                RotationState.player_id == HURT, RotationState.game_date == DAY
            )
        ).one()
    assert hurt == "injured"
    assert found == {
        CLIFF: {"minutes_cliff"},
        HURT: set(),
        TRUST: {"earning_trust"},
        GUARANTEE: {"contract_pressure"},
        LATE: set(),
        SHOOTER: {"usage_up_efficiency_down"},
        TWO_WAY: {"contract_pressure"},
        BLEED: {"bleeding"},
        YO_YO: {"yo_yo"},
        TWICE: set(),
        ARRIVED: set(),
        SHORT: set(),
        SAC_BENCH: set(),
    }


def test_flag_details_carry_the_numbers(seeded: sessionmaker[Session]) -> None:
    compute_flags(seeded, DAY, FlagRule(two_way_limit=24))

    found = _flags(seeded)
    # 5 x 300 of 5 x 14,400 seconds, against 15 x 1,200 + 5 x 300 of 20 x 14,400.
    assert found[CLIFF]["minutes_cliff"] == {
        "share_5": pytest.approx(0.0208, abs=1e-4),
        "share_20": pytest.approx(0.0677, abs=1e-4),
        "ratio": pytest.approx(0.3077, abs=1e-4),
    }
    assert found[GUARANTEE]["contract_pressure"] == {
        "reason": "guarantee",
        "guarantee_date": (DAY + timedelta(days=30)).isoformat(),
        "days": 30,
        "guaranteed": 0,
        "guaranteed_after": None,
    }
    # Active in 19 games: the DNP-CD counts, the inactive game does not.
    assert found[TWO_WAY]["contract_pressure"] == {
        "reason": "two_way",
        "active_games": 19,
        "limit": 24,
        "remaining": 5,
    }
    # 20-28 on 250 possessions with him, 30-20 on 250 without.
    assert found[BLEED]["bleeding"] == {
        "on_court_net": pytest.approx(-32.0),
        "possessions": 250,
        "off_court_net": pytest.approx(40.0),
    }
    # Usage 0.32 over the last 10 games against 0.24 over the season; TS 0.5 against 0.667.
    usage = found[SHOOTER]["usage_up_efficiency_down"]
    assert usage["usg_pct_delta_season"] == pytest.approx(0.08, abs=1e-4)
    assert usage["ts_pct_delta_season"] == pytest.approx(-0.1667, abs=1e-4)
    assert found[TRUST]["earning_trust"] == {
        "shares": [
            pytest.approx(600 / GAME_SECONDS, abs=1e-4),
            pytest.approx(900 / GAME_SECONDS, abs=1e-4),
            pytest.approx(1200 / GAME_SECONDS, abs=1e-4),
        ]
    }
    # The arrival on game 1 is not among them; game 10's row is the first not short.
    assert found[YO_YO]["yo_yo"] == {
        "events": 3,
        "changes": [
            {"game_date": _day(10).isoformat(), "from_tier": "fringe", "to_tier": "core"},
            {"game_date": _day(14).isoformat(), "from_tier": "core", "to_tier": "fringe"},
            {"game_date": _day(20).isoformat(), "from_tier": "fringe", "to_tier": "core"},
        ],
    }


def test_a_yo_yo_counts_neither_an_arrival_nor_a_change_on_a_short_window(
    seeded: sessionmaker[Session],
) -> None:
    with seeded() as session:
        short = dict(
            session.execute(
                select(RotationState.game_date, RotationState.is_short).where(
                    RotationState.player_id == SHORT
                )
            ).all()
        )
    assert (short[_day(5)], short[_day(9)], short[_day(10)]) == (True, True, False)

    compute_flags(seeded, DAY, FlagRule(yo_yo_events=2))

    found = _flags(seeded)
    assert [c["game_date"] for c in found[ARRIVED]["yo_yo"]["changes"]] == [
        _day(14).isoformat(),
        _day(18).isoformat(),
    ]
    assert [c["game_date"] for c in found[SHORT]["yo_yo"]["changes"]] == [
        _day(14).isoformat(),
        _day(18).isoformat(),
    ]


def test_a_two_way_six_games_from_the_limit_is_not_flagged(
    seeded: sessionmaker[Session],
) -> None:
    compute_flags(seeded, DAY, FlagRule(two_way_limit=25))

    assert _flags(seeded)[TWO_WAY] == {}


def test_a_rerun_leaves_identical_rows(seeded: sessionmaker[Session]) -> None:
    rule = FlagRule(two_way_limit=24)
    first = compute_flags(seeded, DAY, rule)
    before = _flags(seeded)

    again = compute_flags(seeded, DAY, rule)

    assert again == {"flags": first["flags"], "removed": first["flags"]}
    assert _flags(seeded) == before

import statistics
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import (
    Game,
    InjuryStatus,
    PlayerGame,
    RosterMembership,
    RotationEvent,
    RotationState,
    Transaction,
)
from closinglineup.derive.events import (
    InjuryChange,
    SignalInputs,
    SignalRule,
    TransactionNote,
    compute_events,
    signals,
)
from closinglineup.derive.rotation import TierRule, compute_rotation_state
from closinglineup.derive.windows import refresh_windows
from closinglineup.sources.injury_pdf import EASTERN

RULE = SignalRule()
# The date of the events the unit tests rank signals for.
ON = date(2026, 10, 25)


def _names(found: list[dict[str, Any]]) -> list[str]:
    return [signal["name"] for signal in found]


def test_signals_rank_by_magnitude_against_the_players_own_sd() -> None:
    # A share drop of 3 sds outranks a starter-rate drop of 2 and a net swing of 1.5; the
    # volatility change of 0.5 sd misses the top three.
    inputs = SignalInputs(
        share_delta=-0.06,
        share_sd=0.02,
        volatility_delta=1.0,
        minutes_sd=2.0,
        starter_delta=-0.8,
        starter_sd=0.4,
        net_delta=-12.0,
        net_sd=8.0,
    )

    assert signals(inputs, RULE, ON) == [
        {"name": "minutes_share", "value": -0.06, "label": "minutes share down 6 pts vs 20-game"},
        {"name": "starter_rate", "value": -0.8, "label": "starter rate down 80 pts vs 20-game"},
        {
            "name": "on_court_net",
            "value": -12.0,
            "label": "on-court net down 12.0 vs prior 10 games",
        },
    ]


def test_a_missing_or_zero_change_or_sd_is_no_signal() -> None:
    inputs = SignalInputs(
        share_delta=0.0,
        share_sd=0.02,
        volatility_delta=2.5,
        minutes_sd=None,
        starter_delta=None,
        starter_sd=0.4,
        usage_delta=0.041,
        usage_sd=0.02,
    )

    assert signals(inputs, RULE, ON) == [
        {"name": "usage", "value": 0.041, "label": "usage up 4.1 pts vs prior 10 games"},
    ]


@pytest.mark.parametrize(
    ("change", "label"),
    [
        # Read the morning after or later, a change on the event's date is dated, not "today".
        (InjuryChange("active", "out", 0), "listed out on Oct 25"),
        (InjuryChange("doubtful", "active", 1), "back from doubtful 1 day before"),
        (InjuryChange("questionable", "out", 4), "status questionable → out 4 days before"),
    ],
)
def test_an_injury_change_reads_as_plain_english(change: InjuryChange, label: str) -> None:
    found = signals(SignalInputs(injury=change), RULE, ON)

    assert found == [
        {"name": "injury", "value": f"{change.before} → {change.after}", "label": label}
    ]


def test_categorical_signals_rank_by_their_fixed_magnitudes_and_ties_keep_spec_order() -> None:
    inputs = SignalInputs(
        share_delta=-0.05,
        share_sd=0.05,
        injury=InjuryChange("out", "active", 0),
        transaction=TransactionNote("trade", 2),
    )

    # The defaults: injury 1.5, the share's 1 sd, then the transaction's 1, the tie in spec order.
    assert _names(signals(inputs, RULE, ON)) == ["injury", "minutes_share", "transaction"]
    tied = SignalRule(injury_magnitude=1.0, transaction_magnitude=1.0)
    assert _names(signals(inputs, tied, ON)) == ["minutes_share", "injury", "transaction"]
    assert signals(inputs, SignalRule(top=1), ON) == [
        {"name": "injury", "value": "out → active", "label": "back from out on Oct 25"}
    ]


# SAC's game g, 1 to 20, is on _day(g) against POR, in a season no other test uses.
SEASON = "2018-19"
FIRST = date(2018, 11, 1)
SAC = 1610612758
POR = 1610612757
GAMES = range(1, 21)
DAY = FIRST + timedelta(days=19)
GAME_SECONDS = 14400

# CLIFF plays 1,044 seconds (17.4 minutes) off the bench in games 1 to 13, then 180 in 14 to 20:
# Core through game 19, his 10-game share 5,256 / 144,000, and Fringe at 20, 4,392 / 144,000.
CLIFF = 970001
CLIFF_HIGH, CLIFF_LOW = 1044, 180
ARRIVAL = 970002  # Signs before game 20 and starts it.
RETURN = 970003  # Starts 1 to 14, out injured 15 to 19, converted from a two-way at 17, starts 20.
STEADY = 970004  # 20 minutes off the bench every game: Core throughout.
SAC_BENCH = 970005  # The rest of SAC's minutes, starting every game.
POR_BENCH = 970006
PLAYERS = [CLIFF, ARRIVAL, RETURN, STEADY, SAC_BENCH, POR_BENCH]
OUT_GAMES = range(15, 20)


def _day(g: int) -> date:
    return FIRST + timedelta(days=g - 1)


def _game_id(g: int) -> str:
    return f"00218009{g:02d}"


def _seconds(player_id: int, g: int) -> tuple[int | None, bool, str | None]:
    """(seconds, started, DNP reason) of a named SAC player in game g; None for no line."""
    if player_id == CLIFF:
        return (CLIFF_HIGH if g <= 13 else CLIFF_LOW), False, None
    if player_id == ARRIVAL:
        return (1500, True, None) if g == 20 else (None, False, None)
    if player_id == RETURN:
        return (0, False, "injury") if g in OUT_GAMES else (1800, True, None)
    return 1200, False, None


def _line(
    g: int,
    player_id: int,
    team_id: int,
    seconds: int,
    *,
    starter: bool = False,
    dnp_reason: str | None = None,
) -> dict[str, Any]:
    counts = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl")
    return {
        "game_id": _game_id(g),
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
    for player_id in (CLIFF, ARRIVAL, RETURN, STEADY):
        seconds, started, dnp = _seconds(player_id, g)
        if seconds is None:
            continue
        used += seconds
        lines.append(_line(g, player_id, SAC, seconds, starter=started, dnp_reason=dnp))
    lines.append(_line(g, SAC_BENCH, SAC, GAME_SECONDS - used, starter=True))
    lines.append(_line(g, POR_BENCH, POR, GAME_SECONDS, starter=True))
    return lines


def _membership(player_id: int, team_id: int, start: date) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "start_date": start,
        "end_date": None,
        "contract_type": "standard",
        "source": "player_game",
    }


def _transaction(player_id: int, day: date, kind: str) -> dict[str, Any]:
    return {
        "date": day,
        "type": kind,
        "player_id": player_id,
        "from_team_id": None,
        "to_team_id": SAC,
        "detail": f"test {kind}",
    }


def _report(g: int, player_id: int, status: str) -> dict[str, Any]:
    """`player_id` on SAC's 10:00 injury report for game g."""
    return {
        "report_datetime": datetime.combine(_day(g), time(10), EASTERN),
        "game_date": _day(g),
        "player_id": player_id,
        "team_id": SAC,
        "status": status,
        "reason": "Injury/Illness - Left Ankle; Sprain",
        "source": "pdf",
    }


def _clear(db: sessionmaker[Session]) -> None:
    game_ids = [_game_id(g) for g in GAMES]
    with db.begin() as session:
        for model in (RotationEvent, RotationState, InjuryStatus, Transaction, RosterMembership):
            session.execute(delete(model).where(model.player_id.in_(PLAYERS)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """SAC's and POR's 20 games, roster spans, RETURN's injury reports and the transactions, the
    view refreshed over them, and rotation state written for games 13 to 20."""
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
                *(_membership(player_id, SAC, start) for player_id in (CLIFF, RETURN, STEADY)),
                _membership(SAC_BENCH, SAC, start),
                _membership(ARRIVAL, SAC, DAY),
                _membership(POR_BENCH, POR, start),
            ],
        )
        session.execute(insert(InjuryStatus), [_report(g, RETURN, "Out") for g in OUT_GAMES])
        session.execute(
            insert(Transaction),
            [_transaction(RETURN, _day(17), "two_way_convert"), _transaction(ARRIVAL, DAY, "sign")],
        )
    refresh_windows(db)
    for g in range(13, 21):
        compute_rotation_state(db, _day(g), TierRule())
    yield db
    _clear(db)
    refresh_windows(db)


def _events(db: sessionmaker[Session], day: date = DAY) -> dict[int, RotationEvent]:
    with db() as session:
        found = session.scalars(select(RotationEvent).where(RotationEvent.game_date == day))
        return {event.player_id: event for event in found if event.player_id in PLAYERS}


def test_a_minutes_cliff_ranks_the_minutes_share_signal_first(
    seeded: sessionmaker[Session],
) -> None:
    compute_events(seeded, DAY, RULE)

    cliff = _events(seeded)[CLIFF]
    assert (cliff.from_tier, cliff.to_tier, cliff.prev_game_date) == ("core", "fringe", _day(19))
    first, second, *_ = cliff.signals
    assert first == {
        "name": "minutes_share",
        "value": pytest.approx(-0.039),
        "label": "minutes share down 3.9 pts vs 20-game",
    }
    # 13 (CLIFF_LOW - CLIFF_HIGH) / 288,000 over the sd of his 20 per-game shares outranks the
    # volatility drop, his 20-game volatility all of it, which is one sd.
    shares = [(CLIFF_HIGH if g <= 13 else CLIFF_LOW) / GAME_SECONDS for g in GAMES]
    assert 0.039 / statistics.stdev(shares) > 1
    assert second["name"] == "minutes_volatility"
    assert second["label"].startswith("minutes volatility down")


def test_an_arrival_has_no_from_tier_and_an_unchanged_tier_no_event(
    seeded: sessionmaker[Session],
) -> None:
    compute_events(seeded, DAY, RULE)

    events = _events(seeded)
    arrival = events[ARRIVAL]
    assert (arrival.from_tier, arrival.prev_game_date, arrival.to_tier) == (None, None, "starter")
    assert arrival.signals == [
        {"name": "transaction", "value": "sign", "label": "signed on Nov 20"}
    ]
    assert set(events) == {CLIFF, ARRIVAL, RETURN}

    # Game 19 changes no one's tier.
    assert compute_events(seeded, _day(19), RULE) == {"events": 0, "removed": 0}
    assert _events(seeded, _day(19)) == {}


def test_a_return_from_injury_ranks_injury_above_a_transaction_only_when_it_is_larger(
    seeded: sessionmaker[Session],
) -> None:
    compute_events(seeded, DAY, RULE)

    back = _events(seeded)[RETURN]
    assert (back.from_tier, back.to_tier) == ("injured", "starter")
    # His share drop, 0.06875 over a per-game sd of 0.0555, is 1.24 sd: between the injury's 1.5
    # and the transaction's 1.
    assert back.signals == [
        {"name": "injury", "value": "out → active", "label": "back from out on Nov 20"},
        {
            "name": "minutes_share",
            "value": pytest.approx(-0.0688, abs=1e-4),
            "label": "minutes share down 6.9 pts vs 20-game",
        },
        {
            "name": "transaction",
            "value": "two_way_convert",
            "label": "converted from a two-way 3 days before",
        },
    ]

    compute_events(seeded, DAY, SignalRule(injury_magnitude=0.9))

    assert _names(_events(seeded)[RETURN].signals) == ["minutes_share", "transaction", "injury"]


def test_an_injury_change_counts_on_the_first_date_of_the_lookback_and_not_before(
    seeded: sessionmaker[Session],
) -> None:
    # CLIFF is questionable from game 14 to 20, a change on game 14 from his active game 13.
    with seeded.begin() as session:
        session.execute(
            insert(InjuryStatus),
            [_report(g, CLIFF, "Questionable") for g in range(14, 21)],
        )
    for g in range(13, 21):
        compute_rotation_state(seeded, _day(g), TierRule())

    compute_events(seeded, DAY, RULE)

    injury = {
        "name": "injury",
        "value": "active → questionable",
        "label": "listed questionable 6 days before",
    }
    assert injury in _events(seeded)[CLIFF].signals

    compute_events(seeded, DAY, SignalRule(lookback_days=6))

    assert "injury" not in _names(_events(seeded)[CLIFF].signals)


def test_usage_is_measured_over_his_production_window(seeded: sessionmaker[Session]) -> None:
    # RETURN takes 10 of SAC's 50 attempts in games 1 to 10 and 20 of 60 in those he plays after:
    # usage 0.32, then 0.5333.
    with seeded.begin() as session:
        for g in GAMES:
            session.execute(
                update(PlayerGame)
                .where(PlayerGame.game_id == _game_id(g), PlayerGame.player_id == SAC_BENCH)
                .values(fga=40)
            )
            session.execute(
                update(PlayerGame)
                .where(PlayerGame.game_id == _game_id(g), PlayerGame.player_id == RETURN)
                .values(fga=0 if g in OUT_GAMES else 10 if g <= 10 else 20)
            )
    refresh_windows(seeded)

    compute_events(seeded, DAY, RULE)

    usages = [10 * 2880 / (1800 * 50)] * 10 + [20 * 2880 / (1800 * 60)] * 5
    change = usages[-1] - usages[0]
    usage = _events(seeded)[RETURN].signals[0]
    assert change / statistics.stdev(usages) > RULE.injury_magnitude
    assert usage == {
        "name": "usage",
        "value": pytest.approx(change, abs=1e-4),
        "label": "usage up 21.3 pts vs prior 10 games",
    }


def test_a_rerun_writes_the_same_events_and_no_duplicates(seeded: sessionmaker[Session]) -> None:
    first = compute_events(seeded, DAY, RULE)
    before = {
        player_id: (e.prev_game_date, e.from_tier, e.to_tier, e.signals)
        for player_id, e in _events(seeded).items()
    }

    again = compute_events(seeded, DAY, RULE)

    after = {
        player_id: (e.prev_game_date, e.from_tier, e.to_tier, e.signals)
        for player_id, e in _events(seeded).items()
    }
    assert again == {"events": first["events"], "removed": first["events"]}
    assert after == before
    with seeded() as session:
        rows = session.scalars(
            select(RotationEvent.player_id).where(
                RotationEvent.game_date == DAY, RotationEvent.player_id.in_(PLAYERS)
            )
        ).all()
    assert sorted(rows) == sorted(before)

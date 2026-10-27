from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, update
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.api.app import create_app
from closinglineup.api.routers.league import MoverRow, QuietRow, top_movers
from closinglineup.db.models import (
    Game,
    ImpactSnapshot,
    InjuryStatus,
    Player,
    PlayerFlag,
    PlayerGame,
    RosterMembership,
    RotationEvent,
    RotationState,
    Transaction,
)
from closinglineup.derive.windows import refresh_windows
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN

# A season no other test seeds.
SEASON = "2034-35"
FIRST = date(2034, 11, 1)
GAMES = range(1, 13)
BOS = 1610612738
NYK = 1610612752
# Four BOS starters play every minute; the fifth spot is FALLER's for games 1 and 2 and RISER's
# from game 3, so the 10-game window at game 12 (games 3-12) is RISER's and the one before it
# (games 1-2) FALLER's.
STARTERS = [9_500_001, 9_500_002, 9_500_003, 9_500_004]
RISER = 9_500_005
FALLER = 9_500_006
NYK_FIVE = [9_500_007, 9_500_008, 9_500_009, 9_500_010, 9_500_011]
# On BOS's injury reports only.
BACK = 9_500_012
NEWLY_OUT = 9_500_013
STILL_OUT = 9_500_014
ASSIGNED = 9_500_015
NYK_OUT = 9_500_016
PLAYERS = [
    *STARTERS,
    RISER,
    FALLER,
    *NYK_FIVE,
    BACK,
    NEWLY_OUT,
    STILL_OUT,
    ASSIGNED,
    NYK_OUT,
]


def _game_id(g: int) -> str:
    return f"00234{g:05d}"


def _day(g: int) -> date:
    return FIRST + timedelta(days=2 * (g - 1))


LAST = _day(GAMES[-1])
# The day between games 1 and 2, on which no one played and no report was filed.
OFF_DAY = FIRST + timedelta(days=1)
# The day between games 11 and 12, on which no one played but BOS filed a report.
BREAK = LAST - timedelta(days=1)
# The morning after the last game, on which a transaction is filed.
MORNING = LAST + timedelta(days=1)
# FALLER's tier on each BOS game, the yo-yo strip's; he has no row on game 7.
STRIP = {g: ("core" if g % 2 else "fringe") for g in GAMES if g != 7}


def _line(g: int, player_id: int, team_id: int) -> dict[str, Any]:
    counts = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl")
    return {
        "game_id": _game_id(g),
        "player_id": player_id,
        "team_id": team_id,
        "is_starter": True,
        "seconds": 2880,
        "dnp_reason": None,
        **dict.fromkeys((*counts, "blk", "tov", "pf", "plus_minus"), 0),
        "source": "stats",
    }


def _lines(g: int) -> list[dict[str, Any]]:
    fifth = FALLER if g <= 2 else RISER
    return [
        *(_line(g, p, BOS) for p in [*STARTERS, fifth]),
        *(_line(g, p, NYK) for p in NYK_FIVE),
    ]


def _game(g: int) -> dict[str, Any]:
    return {
        "game_id": _game_id(g),
        "season": SEASON,
        "game_date": _day(g),
        "phase": "regular",
        "home_team_id": BOS,
        "away_team_id": NYK,
        "status": "final",
        "home_score": 100,
        "away_score": 95,
        "home_b2b": False,
        "away_b2b": False,
    }


def _membership(player_id: int, team_id: int) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "start_date": FIRST - timedelta(days=30),
        "end_date": None,
        "contract_type": "standard",
        "source": "player_game",
    }


def _injury(player_id: int, team_id: int, at: datetime, status: str, reason: str) -> dict[str, Any]:
    return {
        "report_datetime": at,
        "game_date": at.date(),
        "player_id": player_id,
        "team_id": team_id,
        "status": status,
        "reason": reason,
        "source": "pdf",
    }


def _impact(player_id: int, day: date, source: str, value: float) -> dict[str, Any]:
    return {"date": day, "player_id": player_id, "source": source, "value": value}


def _state(player_id: int, team_id: int, day: date, tier: str) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "game_date": day,
        "tier": tier,
        "minutes_share_10": None,
        "starter_rate_5": None,
        "played_rate_10": None,
        "availability": "active",
        "badges": [],
        "is_short": False,
        "inputs": {},
    }


SIGNALS = [
    {"name": "minutes_share", "value": 0.2, "label": "minutes share up 20 pts"},
    {"name": "injury", "value": "Out → Available", "label": "back from injury 2 days ago"},
    {"name": "transaction", "value": "sign", "label": "signed today"},
]


def _event(
    player_id: int, team_id: int, day: date, from_tier: str | None, to_tier: str
) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "game_date": day,
        "prev_game_date": None if from_tier is None else day - timedelta(days=2),
        "from_tier": from_tier,
        "to_tier": to_tier,
        "signals": SIGNALS,
    }


def _flag(player_id: int, team_id: int, day: date, flag: str) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "game_date": day,
        "flag": flag,
        "detail": {"share_5": 0.1},
    }


def clear(db: sessionmaker[Session]) -> None:
    game_ids = [_game_id(g) for g in GAMES]
    with db.begin() as session:
        for model in (
            RotationEvent,
            RotationState,
            PlayerFlag,
            InjuryStatus,
            Transaction,
            ImpactSnapshot,
            RosterMembership,
        ):
            session.execute(delete(model).where(model.player_id.in_(PLAYERS)))
        session.execute(delete(Player).where(Player.nba_player_id.in_(PLAYERS)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """BOS's 12 games against NYK, every other day, with box scores and rosters; impact snapshots
    on the first and last dates and the morning after; on the last, three rotation events, three
    flags (FALLER's yo-yo with his `rotation_state` over BOS's games), a transaction and a BOS
    injury report, each with an earlier one beside it, and a transaction the morning after; the
    view refreshed."""
    clear(db)
    before = datetime.combine(_day(11), time(17), EASTERN)
    on_break = datetime.combine(BREAK, time(17), EASTERN)
    report = datetime.combine(LAST, time(17), EASTERN)
    with db.begin() as session:
        session.execute(insert(Game), [_game(g) for g in GAMES])
        session.execute(insert(PlayerGame), [line for g in GAMES for line in _lines(g)])
        session.execute(
            insert(RosterMembership),
            [
                *(_membership(p, BOS) for p in [*STARTERS, RISER, FALLER]),
                *(_membership(p, NYK) for p in NYK_FIVE),
            ],
        )
        session.execute(
            insert(Player),
            [{"nba_player_id": p, "name": f"Player {p}"} for p in PLAYERS],
        )
        session.execute(
            insert(ImpactSnapshot),
            [
                # RISER's EPM is new on the last date, so his rise is DARKO's.
                _impact(RISER, FIRST, "darko", 1.0),
                _impact(RISER, LAST, "darko", 3.0),
                _impact(RISER, LAST, "epm", 5.0),
                _impact(FALLER, FIRST, "bpm_local", 2.0),
                _impact(FALLER, LAST, "bpm_local", -1.0),
                _impact(STARTERS[0], FIRST, "epm", 1.0),
                _impact(STARTERS[0], LAST, "epm", 1.5),
                # Unchanged: neither a riser nor a faller.
                _impact(STARTERS[1], FIRST, "darko", 0.5),
                # His DARKO is older than his local BPM, but comes first.
                _impact(NYK_FIVE[0], FIRST, "darko", 0.8),
                _impact(NYK_FIVE[0], LAST, "bpm_local", 1.2),
                # After the date, so none on it.
                _impact(NEWLY_OUT, MORNING, "darko", 4.0),
            ],
        )
        session.execute(
            insert(RotationState),
            [_state(FALLER, BOS, _day(g), tier) for g, tier in STRIP.items()],
        )
        session.execute(
            insert(RotationEvent),
            [
                _event(RISER, BOS, LAST, "fringe", "starter"),
                _event(FALLER, BOS, LAST, "core", "out"),
                _event(NYK_FIVE[0], NYK, LAST, None, "starter"),
                _event(RISER, BOS, _day(11), "out", "fringe"),
            ],
        )
        session.execute(
            insert(PlayerFlag),
            [
                _flag(RISER, BOS, LAST, "earning_trust"),
                _flag(FALLER, BOS, LAST, "minutes_cliff"),
                _flag(FALLER, BOS, LAST, "yo_yo"),
                _flag(FALLER, BOS, _day(11), "yo_yo"),
            ],
        )
        session.execute(
            insert(Transaction),
            [
                {
                    "date": LAST,
                    "type": "sign",
                    "player_id": NEWLY_OUT,
                    "from_team_id": None,
                    "to_team_id": BOS,
                    "detail": "Boston signed Player 9500013.",
                },
                {
                    "date": _day(11),
                    "type": "waive",
                    "player_id": BACK,
                    "from_team_id": NYK,
                    "to_team_id": None,
                    "detail": "New York waived Player 9500012.",
                },
                {
                    "date": MORNING,
                    "type": "waive",
                    "player_id": STILL_OUT,
                    "from_team_id": BOS,
                    "to_team_id": None,
                    "detail": "Boston waived Player 9500014.",
                },
                {
                    "date": MORNING + timedelta(days=1),
                    "type": "sign",
                    "player_id": STILL_OUT,
                    "from_team_id": None,
                    "to_team_id": NYK,
                    "detail": "New York signed Player 9500014.",
                },
            ],
        )
        session.execute(
            insert(InjuryStatus),
            [
                _injury(BACK, BOS, before, "Out", "Injury/Illness - Left Ankle; Sprain"),
                _injury(NEWLY_OUT, BOS, before, "Questionable", "Illness"),
                _injury(STILL_OUT, BOS, before, "Out", "Injury/Illness - Right Knee"),
                # Filed on the break, it is a change on the next game date's page.
                _injury(NEWLY_OUT, BOS, on_break, "Out", "Illness"),
                _injury(STILL_OUT, BOS, on_break, "Out", "Injury/Illness - Right Knee"),
                _injury(NEWLY_OUT, BOS, report, "Out", "Illness"),
                _injury(STILL_OUT, BOS, report, "Out", "Injury/Illness - Right Knee"),
                _injury(ASSIGNED, BOS, report, "Out", "G League - On Assignment"),
                # NYK's report is the day before's, so nothing has changed on it.
                _injury(NYK_OUT, NYK, before, "Out", "Rest"),
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


def _page(client: TestClient, day: date = LAST) -> dict[str, Any]:
    response = client.get("/api/league", params={"date": day.isoformat()})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(movers: list[dict[str, Any]]) -> list[int]:
    return [mover["player"]["player_id"] for mover in movers]


def test_movers_rank_by_their_change_and_keep_the_top_n() -> None:
    movers = [MoverRow(p, BOS, 0.0, delta) for p, delta in enumerate([*range(-20, 21), 0.5])]

    risers, fallers = top_movers(movers, 15)

    assert len(risers) == len(fallers) == 15
    assert [mover.delta for mover in risers] == [*range(20, 5, -1)]
    assert [mover.delta for mover in fallers] == [*range(-20, -5)]


def test_a_mover_whose_value_did_not_move_is_neither() -> None:
    assert top_movers([MoverRow(1, BOS, 0.2, 0.0)], 15) == ([], [])


def test_every_rotation_event_on_the_date_appears_once_with_its_signals(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client)

    seen = [
        (team["tricode"], event["player"]["player_id"], event["from_tier"], event["to_tier"])
        for team in page["rotation_events"]
        for event in team["events"]
    ]
    assert seen == [
        ("BOS", RISER, "fringe", "starter"),
        ("BOS", FALLER, "core", "out"),
        ("NYK", NYK_FIVE[0], None, "starter"),
    ]
    for team in page["rotation_events"]:
        for event in team["events"]:
            assert event["signals"] == SIGNALS
            assert event["player"]["name"] is not None


def test_minutes_movers_are_the_10_game_shares_change(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client)

    assert _ids(page["minutes_risers"]) == [RISER]
    assert _ids(page["minutes_fallers"]) == [FALLER]
    riser, faller = page["minutes_risers"][0], page["minutes_fallers"][0]
    assert (riser["tricode"], riser["source"]) == ("BOS", None)
    assert riser["value"] == pytest.approx(0.2)
    assert riser["delta"] == pytest.approx(0.2)
    assert faller["value"] == pytest.approx(0.0)
    assert faller["delta"] == pytest.approx(-0.2)


def test_impact_movers_name_the_first_source_with_a_change(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client)

    risers = [(m["player"]["player_id"], m["source"], m["delta"]) for m in page["impact_risers"]]
    fallers = [(m["player"]["player_id"], m["source"], m["delta"]) for m in page["impact_fallers"]]
    assert risers == [(RISER, "darko", 2.0), (STARTERS[0], "epm", 0.5)]
    assert fallers == [(FALLER, "bpm_local", -3.0)]
    assert page["impact_risers"][0]["value"] == 3.0
    # FALLER has no box line on the date, but his team played.
    assert {m["tricode"] for m in (*page["impact_risers"], *page["impact_fallers"])} == {"BOS"}


def test_flags_are_the_date_s_by_flag(seeded: sessionmaker[Session], client: TestClient) -> None:
    page = _page(client)

    assert [(f["flag"], f["player"]["player_id"], f["tricode"]) for f in page["flags"]] == [
        ("minutes_cliff", FALLER, "BOS"),
        ("earning_trust", RISER, "BOS"),
        ("yo_yo", FALLER, "BOS"),
    ]
    assert page["flags"][0]["detail"] == {"share_5": 0.1}


def test_a_yo_yo_flag_carries_his_tier_on_each_team_game_date_the_oldest_first(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    flags = _page(client)["flags"]

    (yo_yo,) = (flag for flag in flags if flag["flag"] == "yo_yo")
    # Game 7, on which he has no row, is kept in its place with no tier, for the strip's gap.
    assert yo_yo["tiers"] == [
        {"game_date": _day(g).isoformat(), "tier": STRIP.get(g)} for g in GAMES
    ]
    assert [flag["tiers"] for flag in flags if flag["flag"] != "yo_yo"] == [None, None]


def test_every_player_row_carries_his_impact_in_the_first_source_with_a_value(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client)

    rows = [
        *(event for team in page["rotation_events"] for event in team["events"]),
        *page["minutes_risers"],
        *page["minutes_fallers"],
        *page["impact_risers"],
        *page["impact_fallers"],
        *page["flags"],
        *page["transactions"],
    ]
    impacts = {row["player"]["player_id"]: row["impact"] for row in rows}
    assert impacts == {
        # His latest of each source on or before the date, DARKO before local BPM; EPM, cancelled,
        # is never one.
        RISER: {"source": "darko", "value": 3.0},
        FALLER: {"source": "bpm_local", "value": -1.0},
        STARTERS[0]: None,
        NYK_FIVE[0]: {"source": "darko", "value": 0.8},
        NEWLY_OUT: None,
        STILL_OUT: None,
    }
    # Each player's rows agree.
    assert all(row["impact"] == impacts[row["player"]["player_id"]] for row in rows)


def test_injury_changes_are_new_outs_then_returns(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    changes = _page(client)["injury_changes"]

    assert [
        (c["player"]["player_id"], c["from_status"], c["to_status"], c["reason"]) for c in changes
    ] == [
        (NEWLY_OUT, "Questionable", "Out", "Illness"),
        (BACK, "Out", None, "Injury/Illness - Left Ankle; Sprain"),
    ]
    assert {c["tricode"] for c in changes} == {"BOS"}
    report = datetime.fromisoformat(changes[0]["report_datetime"])
    assert report == datetime.combine(LAST, time(17), EASTERN)


def test_transactions_are_the_date_s_and_the_morning_after_s(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    transactions = _page(client)["transactions"]

    assert [
        (t["player"]["player_id"], t["date"], t["type"], t["from_tricode"], t["to_tricode"])
        for t in transactions
    ] == [
        (NEWLY_OUT, LAST.isoformat(), "sign", None, "BOS"),
        (STILL_OUT, MORNING.isoformat(), "waive", "BOS", None),
    ]


def test_a_date_with_no_games_has_empty_lists(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client, OFF_DAY)

    assert page["date"] == OFF_DAY.isoformat()
    assert all(page[key] == [] for key in page if key != "date")


# On BOS's last game, against averages of 48 minutes over games 2-11: a Starter who sat by coach's
# decision, a Core player at 40% and one at 70% of his minutes, a Core player at 40% with a
# rotation event on the date, a Fringe NYK player at 40%, and a Core NYK player out injured.
DNP_CD, LOW, ENOUGH, MOVED = STARTERS
FRINGE, INJURED = NYK_FIVE[1:3]
QUIET_LINES = {
    DNP_CD: (0, "cd"),
    LOW: (1152, None),
    ENOUGH: (2016, None),
    MOVED: (1152, None),
    FRINGE: (1152, None),
    INJURED: (0, "injury"),
}


@pytest.fixture
def quiet(seeded: sessionmaker[Session]) -> sessionmaker[Session]:
    """`seeded`, with the last game's lines and tiers of `QUIET_LINES`' players, MOVED's event and
    LOW's impact on the date; the view refreshed. `seeded` clears it all."""
    tiers = {
        DNP_CD: "starter",
        LOW: "core",
        ENOUGH: "core",
        MOVED: "core",
        FRINGE: "fringe",
        INJURED: "core",
    }
    with seeded.begin() as session:
        for player_id, (seconds, dnp_reason) in QUIET_LINES.items():
            session.execute(
                update(PlayerGame)
                .where(PlayerGame.game_id == _game_id(GAMES[-1]))
                .where(PlayerGame.player_id == player_id)
                .values(seconds=seconds, dnp_reason=dnp_reason)
            )
        session.execute(
            insert(RotationState),
            [_state(p, NYK if p in NYK_FIVE else BOS, LAST, tier) for p, tier in tiers.items()],
        )
        session.execute(insert(RotationEvent), [_event(MOVED, BOS, LAST, "starter", "core")])
        session.execute(insert(ImpactSnapshot), [_impact(LOW, LAST, "darko", 2.5)])
    refresh_windows(seeded)
    return seeded


def test_quiet_signals_are_starters_and_core_well_short_of_their_minutes_a_dnp_first(
    quiet: sessionmaker[Session], client: TestClient
) -> None:
    signals = _page(client)["quiet_signals"]

    assert [
        (s["player"]["player_id"], s["tricode"], s["tier"], s["seconds"], s["dnp_reason"])
        for s in signals
    ] == [(DNP_CD, "BOS", "starter", 0, "cd"), (LOW, "BOS", "core", 1152, None)]
    # The average is over the games before the date's.
    assert [s["minutes_per_game_10"] for s in signals] == [48.0, 48.0]
    assert [s["impact"] for s in signals] == [None, {"source": "darko", "value": 2.5}]


def test_quiet_signals_put_a_dnp_before_a_larger_shortfall() -> None:
    dnp = QuietRow(1, BOS, "core", 0, 10.0, "cd")
    short = QuietRow(2, BOS, "starter", 60, 40.0, None)
    shorter = QuietRow(3, BOS, "starter", 600, 40.0, None)

    assert sorted([shorter, short, dnp], key=QuietRow.rank) == [dnp, short, shorter]


def test_a_date_with_no_games_has_no_quiet_signals(
    quiet: sessionmaker[Session], client: TestClient
) -> None:
    assert _page(client, OFF_DAY)["quiet_signals"] == []

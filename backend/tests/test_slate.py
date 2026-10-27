from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.api.app import create_app
from closinglineup.api.routers.slate import line_change
from closinglineup.db.models import (
    Game,
    ImpactSnapshot,
    InjuryStatus,
    LoadLog,
    Player,
    RosterMembership,
    RotationState,
)
from closinglineup.derive.run import WRITES
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN

# A season no other test seeds, earlier than `test_api_meta`'s derived dates, which must stay the
# latest.
SEASON = "2013-14"
LAST_NIGHT = date(2013, 11, 9)
MORNING = LAST_NIGHT + timedelta(days=1)
BOS = 1610612738
NYK = 1610612752
PHI = 1610612755
LAL = 1610612747
# Last night's game, and the morning's two: BOS on the second night of a back-to-back hosting NYK
# at 19:30, and PHI hosting LAL at 19:00.
GAMES = {
    "0021300901": (LAST_NIGHT, BOS, NYK, None),
    "0021300902": (MORNING, BOS, NYK, time(19, 30)),
    "0021300903": (MORNING, PHI, LAL, time(19)),
}
BOS_GAME = "0021300902"

# BOS's players, each a kind of change between the day before's report and the morning's.
NEW_OUT = 9_700_001  # off the report, now Out
SAME = 9_700_002  # Out on both
NOW_DOUBTFUL = 9_700_003  # Questionable, now Doubtful
DOWNGRADED = 9_700_004  # Probable, now Questionable
UPGRADED = 9_700_005  # Out, now Questionable
NEW_PROBABLE = 9_700_006  # off the report, now Probable
BACK = 9_700_007  # Doubtful, now Available
OFF = 9_700_008  # Questionable, now off it
ASSIGNED = 9_700_009  # on a G League line on the morning's report
TWO_WAY = 9_700_012  # a two-way player with the affiliate on it: neither a line nor a badge
# NYK's only report is ESPN's.
NYK_OUT = 9_700_010
# PHI has no report for the game; this player's last official line put him on assignment.
PHI_ASSIGNED = 9_700_011
PLAYERS = [
    NEW_OUT,
    SAME,
    NOW_DOUBTFUL,
    DOWNGRADED,
    UPGRADED,
    NEW_PROBABLE,
    BACK,
    OFF,
    ASSIGNED,
    NYK_OUT,
    PHI_ASSIGNED,
    TWO_WAY,
]
# The two Out lines order by tier, the reverse of their names'; the two Questionable lines share a
# tier, so they order by name, the reverse of their IDs'.
NAMES = {
    NEW_OUT: "Zed Out",
    SAME: "Abe Out",
    DOWNGRADED: "Able Questionable",
    UPGRADED: "Baker Questionable",
}
# BOS's tiers and minutes shares last night; NEW_PROBABLE's row is older than the team's latest.
STATES = {
    NEW_OUT: ("starter", 0.7),
    SAME: ("core", 0.4),
    NOW_DOUBTFUL: ("core", 0.3),
    DOWNGRADED: ("fringe", 0.1),
    UPGRADED: ("fringe", 0.1),
    BACK: ("starter", 0.6),
    OFF: ("core", 0.35),
}

PREVIOUS = datetime.combine(LAST_NIGHT, time(17), EASTERN)
NOW = datetime.combine(MORNING, time(13), EASTERN)
# After the morning ends, so not read on it.
LATER = datetime.combine(MORNING + timedelta(days=1), time(1), EASTERN)


def _injury(
    player_id: int,
    team_id: int,
    at: datetime,
    status: str,
    reason: str = "Injury/Illness - Left Ankle; Sprain",
    *,
    source: str = "pdf",
    game_id: str | None = BOS_GAME,
    game_date: date = MORNING,
) -> dict[str, Any]:
    return {
        "report_datetime": at,
        "game_date": game_date,
        "game_id": game_id,
        "player_id": player_id,
        "team_id": team_id,
        "status": status,
        "reason": reason,
        "source": source,
    }


INJURIES = [
    _injury(SAME, BOS, PREVIOUS, "Out"),
    _injury(NOW_DOUBTFUL, BOS, PREVIOUS, "Questionable"),
    _injury(DOWNGRADED, BOS, PREVIOUS, "Probable"),
    _injury(UPGRADED, BOS, PREVIOUS, "Out"),
    _injury(BACK, BOS, PREVIOUS, "Doubtful"),
    _injury(OFF, BOS, PREVIOUS, "Questionable", "Illness"),
    _injury(NEW_OUT, BOS, NOW, "Out"),
    _injury(SAME, BOS, NOW, "Out"),
    _injury(NOW_DOUBTFUL, BOS, NOW, "Doubtful"),
    _injury(DOWNGRADED, BOS, NOW, "Questionable"),
    _injury(UPGRADED, BOS, NOW, "Questionable"),
    _injury(NEW_PROBABLE, BOS, NOW, "Probable"),
    _injury(BACK, BOS, NOW, "Available"),
    _injury(ASSIGNED, BOS, NOW, "Out", "G League - On Assignment"),
    _injury(TWO_WAY, BOS, NOW, "Out", "G League - Two-Way"),
    # ESPN's report at the same time loses the tie to the official one.
    _injury(NEW_OUT, BOS, NOW, "Questionable", source="espn", game_id=None),
    _injury(NEW_OUT, BOS, LATER, "Available"),
    _injury(NYK_OUT, NYK, NOW, "Out", source="espn", game_id=None),
    _injury(
        PHI_ASSIGNED,
        PHI,
        datetime.combine(LAST_NIGHT - timedelta(days=1), time(12), EASTERN),
        "Out",
        "G League - On Assignment",
        game_id=None,
        game_date=LAST_NIGHT - timedelta(days=1),
    ),
]


def _game(game_id: str) -> dict[str, Any]:
    day, home, away, tip = GAMES[game_id]
    return {
        "game_id": game_id,
        "season": SEASON,
        "game_date": day,
        "tip_time": None if tip is None else datetime.combine(day, tip, EASTERN),
        "phase": "regular",
        "home_team_id": home,
        "away_team_id": away,
        "status": "final" if day == LAST_NIGHT else "scheduled",
        "home_score": None,
        "away_score": None,
        "home_b2b": home == BOS and day == MORNING,
        "away_b2b": False,
    }


def _state(player_id: int, day: date, tier: str, share: float) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": BOS,
        "game_date": day,
        "tier": tier,
        "minutes_share_10": share,
        "starter_rate_5": None,
        "played_rate_10": None,
        "availability": "active",
        "badges": [],
        "is_short": False,
        "inputs": {},
    }


def _log(day: date, table: str, step: str) -> dict[str, Any]:
    at = datetime.combine(day + timedelta(days=1), time(4), EASTERN)
    return {
        "table_name": table,
        "game_date": day,
        "step": step,
        "status": "succeeded",
        "started_at": at,
        "finished_at": at,
    }


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        for model in (InjuryStatus, RotationState, ImpactSnapshot, RosterMembership):
            session.execute(delete(model).where(model.player_id.in_(PLAYERS)))
        session.execute(delete(Player).where(Player.nba_player_id.in_(PLAYERS)))
        session.execute(delete(Game).where(Game.game_id.in_(GAMES)))
        session.execute(delete(LoadLog).where(LoadLog.game_date == LAST_NIGHT))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """Last night derived; the morning's two games; BOS's report the day before and the morning's,
    NYK's ESPN's alone, PHI and LAL with none, PHI with a player on assignment; BOS's tiers last
    night and an impact snapshot."""
    _clear(db)
    with db.begin() as session:
        session.execute(insert(Game), [_game(game_id) for game_id in GAMES])
        session.execute(
            insert(Player),
            [{"nba_player_id": p, "name": NAMES.get(p, f"Player {p}")} for p in PLAYERS],
        )
        session.execute(insert(InjuryStatus), INJURIES)
        session.execute(
            insert(RotationState),
            [
                *(_state(p, LAST_NIGHT, tier, share) for p, (tier, share) in STATES.items()),
                _state(NEW_PROBABLE, LAST_NIGHT - timedelta(days=2), "core", 0.3),
                # After last night, so not read.
                _state(NEW_OUT, MORNING, "injured", 0.0),
            ],
        )
        session.execute(
            insert(RosterMembership),
            {
                "player_id": PHI_ASSIGNED,
                "team_id": PHI,
                "start_date": LAST_NIGHT - timedelta(days=30),
                "end_date": None,
                "contract_type": "standard",
                "source": "player_game",
            },
        )
        session.execute(
            insert(ImpactSnapshot),
            {"date": LAST_NIGHT, "player_id": NEW_OUT, "source": "darko", "value": 2.5},
        )
        session.execute(insert(LoadLog), [_log(LAST_NIGHT, *step) for step in WRITES])
    yield db
    _clear(db)


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        yield client


def _slate(client: TestClient, day: date = MORNING) -> dict[str, Any]:
    response = client.get("/api/slate", params={"date": day.isoformat()})
    assert response.status_code == 200, response.text
    return response.json()


def _side(page: dict[str, Any], tricode: str) -> dict[str, Any]:
    (team,) = (
        team
        for game in page["games"]
        for team in (game["home"], game["away"])
        if team["tricode"] == tricode
    )
    return team


@pytest.mark.parametrize(
    ("was", "now", "change"),
    [
        (None, "Out", "new"),
        (None, "Probable", "new"),
        ("Questionable", "Doubtful", "new"),
        ("Available", "Out", "new"),
        ("Out", "Questionable", "upgraded"),
        ("Doubtful", "Available", "upgraded"),
        ("Out", "Doubtful", "upgraded"),
        ("Probable", "Questionable", "downgraded"),
        ("Available", "Probable", "downgraded"),
        ("Doubtful", "Out", None),
        ("Out", "Out", None),
        ("Questionable", "Questionable", None),
        ("Questionable", None, "off"),
        (None, None, None),
    ],
)
def test_a_line_s_change_is_the_spec_s(
    was: str | None, now: str | None, change: str | None
) -> None:
    assert line_change(was, now) == change


def test_the_morning_s_games_by_tip_off_with_back_to_backs(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _slate(client)

    assert page["date"] == MORNING.isoformat()
    assert page["last_night"] == LAST_NIGHT.isoformat()
    assert [
        (
            g["game_id"],
            g["home"]["tricode"],
            g["away"]["tricode"],
            g["home"]["b2b"],
            g["away"]["b2b"],
        )
        for g in page["games"]
    ] == [("0021300903", "PHI", "LAL", False, False), (BOS_GAME, "BOS", "NYK", True, False)]
    tips = [datetime.fromisoformat(game["tip_time"]) for game in page["games"]]
    assert tips == [
        datetime.combine(MORNING, time(19), EASTERN),
        datetime.combine(MORNING, time(19, 30), EASTERN),
    ]


def test_a_team_s_lines_are_its_newest_report_s_against_the_day_before_s(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    bos = _side(_slate(client), "BOS")

    # The official report at the same time as ESPN's, not the one filed after the morning.
    assert datetime.fromisoformat(bos["report"]["report_datetime"]) == NOW
    assert (bos["report"]["source"], bos["report"]["stale"], bos["report"]["submitted"]) == (
        "pdf",
        False,
        True,
    )
    # Out, Doubtful, Questionable, Probable, Available, then by tier and name; a G League badge
    # after them, and the line off the report last.
    assert [
        (
            line["player"]["player_id"],
            line["status"],
            line["previous_status"],
            line["change"],
            line["g_league"],
        )
        for line in bos["lines"]
    ] == [
        (NEW_OUT, "Out", None, "new", False),
        (SAME, "Out", "Out", None, False),
        (NOW_DOUBTFUL, "Doubtful", "Questionable", "new", False),
        (DOWNGRADED, "Questionable", "Probable", "downgraded", False),
        (UPGRADED, "Questionable", "Out", "upgraded", False),
        (NEW_PROBABLE, "Probable", None, "new", False),
        (BACK, "Available", "Doubtful", "upgraded", False),
        (ASSIGNED, None, None, None, True),
        (OFF, None, "Questionable", "off", False),
    ]
    off = bos["lines"][-1]
    assert off["reason"] == "Illness"


def test_a_line_carries_his_tier_and_share_last_night_and_his_impact(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    lines = {line["player"]["player_id"]: line for line in _side(_slate(client), "BOS")["lines"]}

    assert (lines[NEW_OUT]["tier"], lines[NEW_OUT]["minutes_share_10"]) == ("starter", 0.7)
    assert lines[NEW_OUT]["impact"] == {"source": "darko", "value": 2.5}
    assert lines[OFF]["tier"] == "core"
    # His only row is older than the team's latest derived date.
    assert (lines[NEW_PROBABLE]["tier"], lines[NEW_PROBABLE]["minutes_share_10"]) == (None, None)
    assert lines[SAME]["impact"] is None


def test_an_espn_report_is_stale_and_with_none_before_it_every_line_is_new(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    nyk = _side(_slate(client), "NYK")

    assert (nyk["report"]["source"], nyk["report"]["stale"], nyk["report"]["submitted"]) == (
        "espn",
        True,
        True,
    )
    assert [(line["player"]["player_id"], line["change"]) for line in nyk["lines"]] == [
        (NYK_OUT, "new")
    ]


def test_a_team_with_no_report_shows_its_g_league_badges_alone(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _slate(client)
    phi, lal = _side(page, "PHI"), _side(page, "LAL")

    not_submitted = {
        "report_datetime": None,
        "source": None,
        "stale": False,
        "submitted": False,
    }
    assert phi["report"] == lal["report"] == not_submitted
    assert [
        (line["player"]["player_id"], line["status"], line["change"], line["g_league"])
        for line in phi["lines"]
    ] == [(PHI_ASSIGNED, None, None, True)]
    assert lal["lines"] == []
    assert phi["projected_starters"] == lal["projected_starters"] == []


def test_a_date_with_no_games_has_an_empty_slate(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    day = MORNING + timedelta(days=1)

    assert _slate(client, day) == {
        "date": day.isoformat(),
        "last_night": LAST_NIGHT.isoformat(),
        "games": [],
    }


def test_a_morning_before_any_derived_date_has_no_last_night(client: TestClient) -> None:
    assert _slate(client, date(1950, 1, 1))["last_night"] is None

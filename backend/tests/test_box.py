import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from closinglineup.parse.box import classify_dnp, parse_box, parse_seconds
from closinglineup.parse.rows import ParseError, PlayerGameRow

FIXTURES = Path(__file__).parent / "fixtures" / "nba_stats"
# DEN at GSW, 137-131 after one overtime.
GAME = "0022500006"
GSW, DEN = 1610612744, 1610612743
STARTERS = {
    GSW: {202710, 1630228, 203110, 1641764, 201939},
    DEN: {1629661, 203932, 203999, 1631128, 1627750},
}
# Three GSW and five DEN players sat, every one a coach's decision. The traditional box lists
# 13 GSW and 15 DEN players: the active ones.
DNPS = {GSW: {1631218, 1630611, 1630311}, DEN: {1641747, 1630192, 1629618, 1631124, 1641816}}
ROSTER_SIZES = {GSW: 13, DEN: 15}
JIMMY_BUTLER = 202710
# Regulation plus one overtime, five on the floor.
TEAM_SECONDS = (48 + 5) * 60 * 5

type Payload = dict[str, Any]


def _fixture(endpoint: str) -> Payload:
    return json.loads((FIXTURES / endpoint / f"{GAME}.json").read_text())


def _game() -> list[PlayerGameRow]:
    return parse_box(
        _fixture("boxscoretraditionalv3"),
        _fixture("boxscoreadvancedv3"),
        _fixture("boxscorehustlev2"),
        _fixture("boxscoreusagev3"),
    )


def _by_team(rows: list[PlayerGameRow]) -> dict[int, list[PlayerGameRow]]:
    found: dict[int, list[PlayerGameRow]] = {}
    for row in rows:
        found.setdefault(row.team_id, []).append(row)
    return found


def test_every_player_on_both_rosters_has_one_row() -> None:
    rows = _game()
    assert Counter(row.team_id for row in rows) == ROSTER_SIZES
    assert len({row.player_id for row in rows}) == len(rows)
    assert {row.game_id for row in rows} == {GAME}
    assert {row.source for row in rows} == {"stats"}


def test_the_five_starters_are_starters() -> None:
    for team, rows in _by_team(_game()).items():
        assert {row.player_id for row in rows if row.is_starter} == STARTERS[team]


def test_every_dnp_has_a_reason_and_no_one_who_played_has_one() -> None:
    for team, rows in _by_team(_game()).items():
        dnps = [row for row in rows if row.seconds == 0]
        assert {row.player_id for row in dnps} == DNPS[team]
        assert {row.dnp_reason for row in dnps} == {"cd"}
        assert all(row.dnp_reason is None for row in rows if row.seconds > 0)


def test_each_teams_seconds_sum_to_regulation_plus_overtime() -> None:
    for rows in _by_team(_game()).values():
        assert sum(row.seconds for row in rows) == TEAM_SECONDS


def test_a_player_joins_all_four_boxes() -> None:
    butler = next(row for row in _game() if row.player_id == JIMMY_BUTLER)
    assert butler == PlayerGameRow(
        game_id=GAME,
        player_id=JIMMY_BUTLER,
        team_id=GSW,
        is_starter=True,
        seconds=38 * 60 + 34,
        pts=21,
        fgm=6,
        fga=16,
        fg3m=2,
        fg3a=4,
        ftm=7,
        fta=8,
        oreb=3,
        dreb=2,
        ast=6,
        stl=3,
        blk=0,
        tov=0,
        pf=1,
        plus_minus=8,
        ts_pct=0.538,
        efg_pct=0.438,
        usg_pct=0.217,
        ast_pct=0.222,
        tov_pct=0.0,
        oreb_pct=0.073,
        dreb_pct=0.061,
        off_rating=125.6,
        def_rating=115.4,
        pace=97.1,
        deflections=3,
        contested_shots=3,
        charges_drawn=0,
        source="stats",
    )


def test_a_dnp_has_no_advanced_or_hustle_numbers() -> None:
    dnp = next(row for row in _game() if row.player_id == 1631218)
    assert (dnp.ts_pct, dnp.usg_pct, dnp.off_rating, dnp.deflections) == (None, None, None, None)
    assert (dnp.pts, dnp.seconds, dnp.plus_minus) == (0, 0, 0)


def test_the_traditional_box_alone_parses_with_the_rest_null() -> None:
    rows = parse_box(_fixture("boxscoretraditionalv3"))
    assert len(rows) == sum(ROSTER_SIZES.values())
    assert all(row.ts_pct is None and row.deflections is None for row in rows)


def test_a_box_for_another_game_is_refused() -> None:
    advanced = copy.deepcopy(_fixture("boxscoreadvancedv3"))
    advanced["boxScoreAdvanced"]["gameId"] = "0022500007"
    with pytest.raises(ParseError, match="0022500007"):
        parse_box(_fixture("boxscoretraditionalv3"), advanced)


def test_a_missing_count_is_an_error_not_a_zero() -> None:
    traditional = copy.deepcopy(_fixture("boxscoretraditionalv3"))
    del traditional["boxScoreTraditional"]["homeTeam"]["players"][0]["statistics"]["points"]
    with pytest.raises(ValueError, match="pts"):
        parse_box(traditional)


@pytest.mark.parametrize(
    ("minutes", "seconds"),
    [
        ("38:34", 2314),
        ("28:60", 1740),
        ("0:00", 0),
        ("", 0),
        (None, 0),
        ("PT28M59.70S", 1740),
        ("PT04M22.00S", 262),
        ("PT00M00.00S", 0),
    ],
)
def test_seconds_parse_from_either_minutes_form(minutes: str | None, seconds: int) -> None:
    assert parse_seconds(minutes) == seconds


def test_unreadable_minutes_are_an_error() -> None:
    with pytest.raises(ParseError):
        parse_seconds("38m")


@pytest.mark.parametrize(
    ("comment", "reason"),
    [
        ("DNP - Coach's Decision", "cd"),
        ("DND - Injury/Illness", "injury"),
        ("DNP - Injury/Illness", "injury"),
        ("NWT - Injury/Illness", "injury"),
        ("NWT - Personal Reasons", "inactive"),
        ("NWT - Not With Team", "inactive"),
        ("Inactive", "inactive"),
        ("NWT - Suspended", "suspension"),
        ("DNP - G League - On Assignment", "g_league"),
        ("NWT - G League - Two-Way", "g_league"),
        ("", "other"),
        ("DNP - Rest", "other"),
    ],
)
def test_dnp_comments_classify(comment: str, reason: str) -> None:
    assert classify_dnp(comment) == reason

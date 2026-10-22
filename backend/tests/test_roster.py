import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from closinglineup.parse.roster import (
    contract_type,
    parse_players,
    parse_roster,
    participation_spans,
)
from closinglineup.parse.rows import ParseError, PlayerRow, RosterMembershipRow

FIXTURES = Path(__file__).parent / "fixtures" / "nba_stats"
GSW = 1610612744
AS_OF = date(2026, 4, 12)
STEPHEN_CURRY = 201939
# The ten players in the trimmed `commonallplayers` fixture, none of them on the GSW roster.
ALL_PLAYERS_ROWS = 10
GSW_ROSTER_ROWS = 18

type Payload = dict[str, Any]


def _all_players() -> Payload:
    return json.loads((FIXTURES / "commonallplayers" / "2025-26.json").read_text())


def _gsw() -> Payload:
    return json.loads((FIXTURES / "commonteamroster" / f"{GSW}_2025-26.json").read_text())


def test_every_roster_player_has_an_open_standard_membership_from_the_snapshot_date() -> None:
    rows = parse_roster(_gsw(), AS_OF)
    assert len(rows) == GSW_ROSTER_ROWS
    assert {row.team_id for row in rows} == {GSW}
    assert {(row.start_date, row.end_date) for row in rows} == {(AS_OF, None)}
    assert {row.contract_type for row in rows} == {"standard"}
    assert {row.source for row in rows} == {"commonteamroster"}
    assert STEPHEN_CURRY in {row.player_id for row in rows}


@pytest.mark.parametrize(
    ("how_acquired", "kind"),
    [
        ("#7 Pick in 2009 Draft", "standard"),
        ("Signed on 10/01/25", "standard"),
        (None, "standard"),
        ("Signed to a Two-Way Contract on 07/10/25", "two_way"),
        ("Signed two way on 07/10/25", "two_way"),
        ("Signed to a 10-Day Contract on 01/05/26", "ten_day"),
        ("Signed to an Exhibit 10 Contract", "exhibit_10"),
    ],
)
def test_contract_type_reads_the_how_acquired_hint(how_acquired: str | None, kind: str) -> None:
    assert contract_type(how_acquired) == kind


def test_players_come_from_all_players_and_every_roster() -> None:
    rows = parse_players(_all_players(), [_gsw()])
    assert len(rows) == ALL_PLAYERS_ROWS + GSW_ROSTER_ROWS
    assert [row.nba_player_id for row in rows] == sorted(row.nba_player_id for row in rows)


def test_a_roster_fills_in_birthdate_height_position_and_draft_slot() -> None:
    curry = next(r for r in parse_players(_all_players(), [_gsw()]) if r.nba_player_id == 201939)
    assert curry == PlayerRow(
        nba_player_id=STEPHEN_CURRY,
        name="Stephen Curry",
        birthdate=date(1988, 3, 14),
        draft_year=2009,
        draft_pick=7,
        height_in=74,
        position="G",
    )


def test_a_player_on_no_roster_has_only_a_name() -> None:
    first = parse_players(_all_players())[0]
    assert first == PlayerRow(nba_player_id=51, name="Mahmoud Abdul-Rauf")


def test_a_traded_draft_pick_has_no_draft_slot() -> None:
    rows = {r.nba_player_id: r for r in parse_players(_all_players(), [_gsw()])}
    will_richard = rows[1642954]  # "Draft Rights Traded from MEM on 07/06/25"
    assert (will_richard.draft_year, will_richard.draft_pick) == (None, None)
    assert rows[204001].height_in == 86  # Kristaps Porziņģis, 7-2


def test_a_payload_without_the_result_set_is_an_error() -> None:
    with pytest.raises(ParseError, match="CommonTeamRoster"):
        parse_roster(_all_players(), AS_OF)


DEN, LAL = 1610612743, 1610612747
OCT_23, OCT_25, NOV_2, NOV_9, DEC_1 = (
    date(2025, 10, 23),
    date(2025, 10, 25),
    date(2025, 11, 2),
    date(2025, 11, 9),
    date(2025, 12, 1),
)


def _span(player_id: int, team_id: int, start: date, end: date) -> RosterMembershipRow:
    return RosterMembershipRow(
        player_id=player_id, team_id=team_id, start_date=start, end_date=end, source="player_game"
    )


def test_a_player_on_one_team_has_one_span_from_his_first_game_to_his_last() -> None:
    appearances = [(STEPHEN_CURRY, GSW, day) for day in (NOV_2, OCT_23, OCT_25)]

    assert participation_spans(appearances) == [_span(STEPHEN_CURRY, GSW, OCT_23, NOV_2)]


def test_a_player_traded_away_and_back_has_a_span_per_stint_and_none_overlap() -> None:
    appearances = [
        (1, GSW, OCT_23),
        (1, GSW, OCT_25),
        (1, DEN, NOV_2),
        (1, GSW, NOV_9),
        (1, GSW, DEC_1),
        (2, LAL, OCT_25),
    ]

    assert participation_spans(appearances) == [
        _span(1, GSW, OCT_23, OCT_25),
        _span(1, DEN, NOV_2, NOV_2),
        _span(1, GSW, NOV_9, DEC_1),
        _span(2, LAL, OCT_25, OCT_25),
    ]


def test_spans_are_standard_and_ignore_a_repeated_appearance() -> None:
    [span] = participation_spans([(1, GSW, OCT_23), (1, GSW, OCT_23)])

    assert (span.start_date, span.end_date, span.contract_type) == (OCT_23, OCT_23, "standard")


def test_no_appearances_have_no_spans() -> None:
    assert participation_spans([]) == []

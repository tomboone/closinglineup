import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

from closinglineup.parse.rows import GameRow
from closinglineup.parse.schedule import parse_schedule
from closinglineup.season import Phase

FIXTURES = Path(__file__).parent / "fixtures"
SCHEDULE_2025_26 = FIXTURES / "nba_stats" / "scheduleleaguev2" / "2025-26.json"
SCHEDULE_2026_27 = FIXTURES / "cdn" / "scheduleleaguev2" / "2026-27.json"

# Every counted kind of game in the 2025-26 fixture. Its preseason, Rising Stars and All-Star games
# (0012500008, 0012500001, 0032500004, 0032500011) never count, so have no row.
PHASES_2025_26 = {
    "0022500001": Phase.REGULAR,
    "0022500020": Phase.REGULAR,  # NBA Cup group game
    "0022500147": Phase.REGULAR,  # Mexico City Global Game
    "0022501201": Phase.CUP_KNOCKOUT,  # Cup quarterfinal, a 002 game
    "0022501229": Phase.CUP_KNOCKOUT,  # Cup semifinal, a 002 game
    "0062500001": Phase.CUP_KNOCKOUT,  # Cup championship
    "0052500111": Phase.PLAY_IN,
    "0042500121": Phase.PLAYOFF,  # East first round
    "0042500401": Phase.PLAYOFF,  # Finals
}
# SAS at IND on 2027-01-14, then IND at SAS on 2027-01-17, both in Paris.
SAS, IND = 1610612759, 1610612740
PARIS_FIRST, PARIS_SECOND = "0022600075", "0022600076"

type Payload = dict[str, Any]


def _load(path: Path) -> Payload:
    return json.loads(path.read_text())


def _by_id(rows: list[GameRow]) -> dict[str, GameRow]:
    return {row.game_id: row for row in rows}


def _edit_game(payload: Payload, game_id: str, **changes: Any) -> Payload:
    edited = copy.deepcopy(payload)
    for day in edited["leagueSchedule"]["gameDates"]:
        for game in day["games"]:
            if game["gameId"] == game_id:
                game.update(changes)
    return edited


def test_each_counted_game_has_its_phase_and_no_other_game_has_a_row() -> None:
    rows = _by_id(parse_schedule(_load(SCHEDULE_2025_26)))
    assert {game_id: row.phase for game_id, row in rows.items()} == PHASES_2025_26


def test_a_final_game_carries_its_season_date_teams_and_score() -> None:
    opener = _by_id(parse_schedule(_load(SCHEDULE_2025_26)))["0022500001"]
    assert opener == GameRow(
        game_id="0022500001",
        season="2025-26",
        game_date=date(2025, 10, 21),
        phase=Phase.REGULAR,
        home_team_id=1610612760,
        away_team_id=1610612745,
        status="final",
        home_score=125,
        away_score=124,
        home_b2b=False,
        away_b2b=False,
    )


def test_a_scheduled_game_has_no_score() -> None:
    game = _by_id(parse_schedule(_load(SCHEDULE_2026_27)))["0022600015"]
    assert (game.status, game.home_score, game.away_score) == ("scheduled", None, None)


def test_a_game_whose_teams_are_not_set_yet_has_no_row() -> None:
    rows = _by_id(parse_schedule(_load(SCHEDULE_2026_27)))
    assert "0022601201" not in rows  # Cup quarterfinal, teams TBD
    assert "0062600001" not in rows  # Cup championship, teams TBD
    assert "0022600001" in rows


def test_a_postponed_game_is_postponed_with_no_score() -> None:
    payload = _edit_game(_load(SCHEDULE_2025_26), "0022500001", postponedStatus="Y")
    game = _by_id(parse_schedule(payload))["0022500001"]
    assert (game.status, game.home_score, game.away_score) == ("postponed", None, None)


def test_back_to_back_is_a_game_the_day_after_the_teams_last() -> None:
    payload = _load(SCHEDULE_2026_27)
    apart = _by_id(parse_schedule(payload))[PARIS_SECOND]
    assert (apart.home_b2b, apart.away_b2b) == (False, False)

    moved = _edit_game(payload, PARIS_SECOND, gameDateEst="2027-01-15T00:00:00Z")
    rows = _by_id(parse_schedule(moved))
    second = rows[PARIS_SECOND]
    assert (second.home_team_id, second.away_team_id) == (IND, SAS)
    assert (second.home_b2b, second.away_b2b) == (True, True)
    first = rows[PARIS_FIRST]
    assert (first.home_b2b, first.away_b2b) == (False, False)


def test_back_to_back_is_per_team() -> None:
    # SAS's second game a day after its first, against a team that was idle the day before.
    moved = _edit_game(
        _load(SCHEDULE_2026_27),
        "0022600673",  # GSW at SAS, 2027-01-26
        gameDateEst="2027-01-15T00:00:00Z",
    )
    game = _by_id(parse_schedule(moved))["0022600673"]
    assert (game.home_team_id, game.home_b2b, game.away_b2b) == (SAS, True, False)

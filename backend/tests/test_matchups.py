import json
from pathlib import Path
from typing import Any

from closinglineup.parse.matchups import parse_matchups
from closinglineup.parse.rows import MatchupRow

FIXTURE = (
    Path(__file__).parent / "fixtures" / "nba_stats" / "boxscorematchupsv3" / "0022500006.json"
)
GAME = "0022500006"
JIMMY_BUTLER = 202710
CHRISTIAN_BRAUN = 1631128


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_one_row_per_offensive_player_and_defender() -> None:
    payload = _payload()
    box = payload["boxScoreMatchups"]
    listed = sum(
        len(player["matchups"])
        for side in ("homeTeam", "awayTeam")
        for player in box[side]["players"]
    )
    rows = parse_matchups(payload)
    assert len(rows) == listed
    assert len({(row.off_player_id, row.def_player_id) for row in rows}) == len(rows)
    assert {row.game_id for row in rows} == {GAME}


def test_a_matchup_carries_the_offensive_players_numbers_against_that_defender() -> None:
    rows = parse_matchups(_payload())
    found = next(
        row
        for row in rows
        if (row.off_player_id, row.def_player_id) == (JIMMY_BUTLER, CHRISTIAN_BRAUN)
    )
    assert found == MatchupRow(
        game_id=GAME,
        off_player_id=JIMMY_BUTLER,
        def_player_id=CHRISTIAN_BRAUN,
        partial_poss=4.2,
        pts=2,
        fgm=1,
        fga=1,
        fg3m=0,
        fg3a=0,
        tov=0,
        ast=0,
    )

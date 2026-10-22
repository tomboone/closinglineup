from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from closinglineup.db import models
from closinglineup.parse.rows import (
    GameRow,
    MatchupRow,
    PlayerGameRow,
    PlayerRow,
    RosterMembershipRow,
    Row,
)

# The columns the database fills, which no row carries.
DATABASE_FILLED = {"id", "created_at", "updated_at"}

MATCHUP: dict[str, Any] = {
    "game_id": "0022500006",
    "off_player_id": 202710,
    "def_player_id": 1631128,
    "partial_poss": 4.2,
    "pts": 2,
    "fgm": 1,
    "fga": 1,
    "fg3m": 0,
    "fg3a": 0,
    "tov": 0,
    "ast": 0,
}


@pytest.mark.parametrize(
    ("row", "table"),
    [
        (PlayerRow, models.Player),
        (RosterMembershipRow, models.RosterMembership),
        (GameRow, models.Game),
        (PlayerGameRow, models.PlayerGame),
        (MatchupRow, models.Matchup),
    ],
)
def test_a_row_has_its_tables_columns(row: type[Row], table: type[models.Base]) -> None:
    columns = {column.name for column in table.__table__.columns}
    assert set(row.model_fields) == columns - DATABASE_FILLED


@pytest.mark.parametrize("game_id", ["22500006", "00225000060", "002250000a", 22500006, ""])
def test_a_game_id_that_is_not_ten_digits_is_rejected(game_id: object) -> None:
    with pytest.raises(ValidationError, match="game_id"):
        MatchupRow.model_validate({**MATCHUP, "game_id": game_id})


def test_string_typed_numbers_are_coerced() -> None:
    row = MatchupRow.model_validate(
        {**MATCHUP, "off_player_id": "202710", "partial_poss": "4.2", "pts": "2", "ast": 0.0}
    )
    assert row == MatchupRow.model_validate(MATCHUP)


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="minutes"):
        MatchupRow.model_validate({**MATCHUP, "minutes": "38:34"})


def test_a_nullable_column_left_out_is_none() -> None:
    game = GameRow.model_validate(
        {
            "game_id": "0022600015",
            "season": "2026-27",
            "game_date": "2026-10-30",
            "phase": "regular",
            "home_team_id": 1610612764,
            "away_team_id": 1610612766,
            "status": "scheduled",
            "home_b2b": False,
            "away_b2b": False,
        }
    )
    assert (game.game_date, game.home_score, game.away_score) == (date(2026, 10, 30), None, None)

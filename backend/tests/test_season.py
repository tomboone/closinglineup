import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from closinglineup.season import Phase, is_counted, phase, season_label, season_start_year

SCHEDULE_2025_26 = (
    Path(__file__).parent / "fixtures" / "nba_stats" / "scheduleleaguev2" / "2025-26.json"
)

# One recorded game per kind in the trimmed 2025-26 schedule; `None` means never counted.
EXPECTED_2025_26: dict[str, Phase | None] = {
    "0012500008": None,  # preseason, Abu Dhabi Global Game
    "0012500001": None,  # preseason
    "0022500001": Phase.REGULAR,
    "0022500020": Phase.REGULAR,  # NBA Cup East Group A
    "0022500147": Phase.REGULAR,  # Mexico City Global Game
    "0022501201": Phase.CUP_KNOCKOUT,  # Cup East Quarterfinal, a 002 game
    "0022501229": Phase.CUP_KNOCKOUT,  # Cup East Semifinal, a 002 game
    "0062500001": Phase.CUP_KNOCKOUT,  # Cup Championship
    "0032500004": None,  # Rising Stars semifinal
    "0032500011": None,  # All-Star
    "0052500111": Phase.PLAY_IN,
    "0042500121": Phase.PLAYOFF,  # East First Round
    "0042500401": Phase.PLAYOFF,  # NBA Finals
}


@dataclass(frozen=True)
class Game:
    game_id: str
    game_subtype: str = ""


def _fixture_games() -> list[Game]:
    raw: dict[str, Any] = json.loads(SCHEDULE_2025_26.read_text())
    return [
        Game(g["gameId"], g["gameSubtype"])
        for day in raw["leagueSchedule"]["gameDates"]
        for g in day["games"]
    ]


@pytest.mark.parametrize(
    ("d", "start_year", "label"),
    [
        (date(2026, 10, 21), 2026, "2026-27"),
        (date(2027, 6, 1), 2026, "2026-27"),
        (date(2027, 7, 31), 2026, "2026-27"),
        (date(2027, 8, 1), 2027, "2027-28"),
        (date(2099, 10, 1), 2099, "2099-00"),
    ],
)
def test_season_rolls_over_in_august(d: date, start_year: int, label: str) -> None:
    assert season_start_year(d) == start_year
    assert season_label(d) == label


def test_fixture_holds_exactly_the_expected_games() -> None:
    assert sorted(g.game_id for g in _fixture_games()) == sorted(EXPECTED_2025_26)


@pytest.mark.parametrize("game", _fixture_games(), ids=lambda g: g.game_id)
def test_phase_and_counted_from_the_2025_26_schedule(game: Game) -> None:
    expected = EXPECTED_2025_26[game.game_id]

    assert phase(game) is expected
    assert is_counted(game) is (expected is not None)


def test_other_leagues_never_count() -> None:
    # Las Vegas Summer League runs under league ID 15; its game IDs carry that prefix.
    summer_league = Game("1522500001")

    assert phase(summer_league) is None
    assert not is_counted(summer_league)


def test_knockout_subtype_only_upgrades_a_regular_season_game() -> None:
    assert phase(Game("0012500001", "in-season-knockout")) is None

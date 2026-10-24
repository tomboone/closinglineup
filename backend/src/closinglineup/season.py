"""Season naming, game phase, and which games count (spec §2, Counted games).

A season is named by the year it starts and rolls over on August 1; NBA.com labels it `"2026-27"`.
Pass an Eastern-time date: that is the calendar NBA game dates are kept on.

A game's phase comes from its NBA.com game ID, `00` + season type + season year + sequence (e.g.
`0022500001`), plus the schedule's `gameSubtype`. Season types: 1 preseason, 2 regular season,
3 All-Star, 4 playoffs, 5 play-in, 6 NBA Cup championship. The Cup quarterfinals and semifinals
are type-2 games that only the `in-season-knockout` subtype tells apart; Cup group games are plain
regular season.
"""

from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

ROLLOVER_MONTH = 8

NBA_LEAGUE_ID = "00"
CUP_KNOCKOUT_SUBTYPE = "in-season-knockout"


class Phase(StrEnum):
    REGULAR = "regular"
    CUP_KNOCKOUT = "cup_knockout"
    PLAY_IN = "play_in"
    PLAYOFF = "playoff"


# Season-type digit of a counted game's ID. Preseason (1) and All-Star (3) are deliberately absent.
_PHASE_BY_SEASON_TYPE = {
    "2": Phase.REGULAR,
    "4": Phase.PLAYOFF,
    "5": Phase.PLAY_IN,
    "6": Phase.CUP_KNOCKOUT,
}


class ScheduleGame(Protocol):
    """The two schedule fields a game's phase is read from."""

    @property
    def game_id(self) -> str: ...

    @property
    def game_subtype(self) -> str: ...


def season_start_year(d: date) -> int:
    return d.year if d.month >= ROLLOVER_MONTH else d.year - 1


def _label(start: int) -> str:
    return f"{start}-{(start + 1) % 100:02d}"


def espn_season(d: date) -> int:
    """ESPN names a season by the year it ends: 2027 for 2026-27."""
    return season_start_year(d) + 1


def season_label(d: date) -> str:
    return _label(season_start_year(d))


def season_bounds(season: str) -> tuple[date, date]:
    """The first and last calendar dates of a season label: `2025-26` → 2025-08-01, 2026-07-31."""
    start = int(season[:4])
    return date(start, ROLLOVER_MONTH, 1), date(start + 1, ROLLOVER_MONTH, 1) - timedelta(days=1)


def season_of_game_id(game_id: str) -> str:
    """The season a game ID belongs to, from its two-digit season year: `0022500001` → 2025-26."""
    return _label(2000 + int(game_id[3:5]))


def phase(game: ScheduleGame) -> Phase | None:
    """The game's phase, or `None` for one that never counts: preseason, All-Star (including
    Rising Stars), and any other league's game, such as Summer League."""
    if not game.game_id.startswith(NBA_LEAGUE_ID):
        return None
    found = _PHASE_BY_SEASON_TYPE.get(game.game_id[2:3])
    if found is Phase.REGULAR and game.game_subtype == CUP_KNOCKOUT_SUBTYPE:
        return Phase.CUP_KNOCKOUT
    return found


def is_counted(game: ScheduleGame) -> bool:
    return phase(game) is not None

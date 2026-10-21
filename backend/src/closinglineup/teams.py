"""The canonical team table: one row per franchise, keyed by NBA.com team ID, tricode and ESPN ID.

Ported from nba-today. IDs, tricodes and names are NBA.com's, from the 2025-26
`scheduleleaguev2`; ESPN IDs are for the ESPN injuries fallback. Hard-coded, since the set
changes about once a decade.
"""

from collections.abc import Hashable
from dataclasses import dataclass
from typing import Literal

type Conference = Literal["East", "West"]
type Division = Literal["Atlantic", "Central", "Southeast", "Northwest", "Pacific", "Southwest"]


class UnknownTeam(LookupError):
    """No franchise in the table has this key; e.g. a preseason opponent like Hapoel Jerusalem."""


@dataclass(frozen=True, slots=True)
class Team:
    nba_team_id: int
    tricode: str
    name: str
    conference: Conference
    division: Division
    espn_id: int


TEAMS: tuple[Team, ...] = (
    Team(1610612737, "ATL", "Atlanta Hawks", "East", "Southeast", 1),
    Team(1610612738, "BOS", "Boston Celtics", "East", "Atlantic", 2),
    Team(1610612751, "BKN", "Brooklyn Nets", "East", "Atlantic", 17),
    Team(1610612766, "CHA", "Charlotte Hornets", "East", "Southeast", 30),
    Team(1610612741, "CHI", "Chicago Bulls", "East", "Central", 4),
    Team(1610612739, "CLE", "Cleveland Cavaliers", "East", "Central", 5),
    Team(1610612742, "DAL", "Dallas Mavericks", "West", "Southwest", 6),
    Team(1610612743, "DEN", "Denver Nuggets", "West", "Northwest", 7),
    Team(1610612765, "DET", "Detroit Pistons", "East", "Central", 8),
    Team(1610612744, "GSW", "Golden State Warriors", "West", "Pacific", 9),
    Team(1610612745, "HOU", "Houston Rockets", "West", "Southwest", 10),
    Team(1610612754, "IND", "Indiana Pacers", "East", "Central", 11),
    Team(1610612746, "LAC", "LA Clippers", "West", "Pacific", 12),
    Team(1610612747, "LAL", "Los Angeles Lakers", "West", "Pacific", 13),
    Team(1610612763, "MEM", "Memphis Grizzlies", "West", "Southwest", 29),
    Team(1610612748, "MIA", "Miami Heat", "East", "Southeast", 14),
    Team(1610612749, "MIL", "Milwaukee Bucks", "East", "Central", 15),
    Team(1610612750, "MIN", "Minnesota Timberwolves", "West", "Northwest", 16),
    Team(1610612740, "NOP", "New Orleans Pelicans", "West", "Southwest", 3),
    Team(1610612752, "NYK", "New York Knicks", "East", "Atlantic", 18),
    Team(1610612760, "OKC", "Oklahoma City Thunder", "West", "Northwest", 25),
    Team(1610612753, "ORL", "Orlando Magic", "East", "Southeast", 19),
    Team(1610612755, "PHI", "Philadelphia 76ers", "East", "Atlantic", 20),
    Team(1610612756, "PHX", "Phoenix Suns", "West", "Pacific", 21),
    Team(1610612757, "POR", "Portland Trail Blazers", "West", "Northwest", 22),
    Team(1610612758, "SAC", "Sacramento Kings", "West", "Pacific", 23),
    Team(1610612759, "SAS", "San Antonio Spurs", "West", "Southwest", 24),
    Team(1610612761, "TOR", "Toronto Raptors", "East", "Atlantic", 28),
    Team(1610612762, "UTA", "Utah Jazz", "West", "Northwest", 26),
    Team(1610612764, "WAS", "Washington Wizards", "East", "Southeast", 27),
)

_BY_NBA_TEAM_ID = {t.nba_team_id: t for t in TEAMS}
_BY_TRICODE = {t.tricode: t for t in TEAMS}
_BY_ESPN_ID = {t.espn_id: t for t in TEAMS}


def _lookup[K: Hashable](index: dict[K, Team], key: K, kind: str) -> Team:
    try:
        return index[key]
    except KeyError:
        raise UnknownTeam(f"no team with {kind} {key!r}") from None


def by_nba_team_id(nba_team_id: int) -> Team:
    return _lookup(_BY_NBA_TEAM_ID, nba_team_id, "NBA team ID")


def by_tricode(tricode: str) -> Team:
    return _lookup(_BY_TRICODE, tricode, "tricode")


def by_espn_id(espn_id: int) -> Team:
    """ESPN sends IDs as strings (`"18"`); coerce them to int before calling this."""
    return _lookup(_BY_ESPN_ID, espn_id, "ESPN ID")

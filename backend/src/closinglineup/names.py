"""Matching a source's spelling of a player's name to his NBA.com player ID.

Sources other than NBA.com's JSON name players rather than identify them: the injury report, and
Hoops Rumors (spec §4.6). A name is matched to `player.name`, NBA.com's `DISPLAY_FIRST_LAST`, with
case, accents and punctuation ignored. Where a source spells a player differently, as the injury
report's "Yang, Hansen" for NBA.com's Yang Hansen does, the alias table `aliases.yml`, beside
this module, maps the source's spelling to his ID.
"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path

import yaml

# In the package, not `backend/data/`, which is the local data volume and is not committed.
ALIASES = Path(__file__).with_name("aliases.yml")

_NOT_LETTER = re.compile(r"[^a-z ]")


def normalize(name: str) -> str:
    """`name` lowercased, without accents or punctuation, a hyphen read as a space."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(_NOT_LETTER.sub("", ascii_name.lower().replace("-", " ")).split())


def first_last(last_comma_first: str) -> str:
    """ "Lively II, Dereck" as "Dereck Lively II"; a name with no comma as it is."""
    last, comma, first = last_comma_first.partition(",")
    return f"{first.strip()} {last.strip()}" if comma else last_comma_first.strip()


def load_aliases(path: Path = ALIASES) -> dict[str, int]:
    """The alias table, `{name: player ID}`, keyed by the normalized name."""
    loaded: object = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} is not a mapping of name to player ID")
    aliases: dict[str, int] = {}
    for name, player_id in loaded.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(name, str) or not isinstance(player_id, int):
            raise ValueError(f"{path.name}: {name!r}: {player_id!r} is not a name and a player ID")
        aliases[normalize(name)] = player_id
    return aliases


class NameIndex:
    """Player IDs by normalized name, over `players` as (player ID, name) and the alias table."""

    def __init__(self, players: Iterable[tuple[int, str]], aliases: Mapping[str, int]) -> None:
        self._ids: defaultdict[str, set[int]] = defaultdict(set)
        for player_id, name in players:
            self._ids[normalize(name)].add(player_id)
        self._aliases = dict(aliases)

    def resolve(self, name: str, team_players: Collection[int] = ()) -> int | None:
        """The ID of the player `name`, first name first, names. None if it names no player, or
        several of whom not exactly one is in `team_players`, his team's on the date."""
        key = normalize(name)
        if key in self._aliases:
            return self._aliases[key]
        found = self._ids.get(key, set())
        if len(found) > 1:
            found = found & set(team_players)
        return next(iter(found)) if len(found) == 1 else None

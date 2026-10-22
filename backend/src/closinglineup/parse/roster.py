"""`commonallplayers` and `commonteamroster` → `PlayerRow`s and `RosterMembershipRow`s (spec §4.1,
§6.1 `player`, `roster_membership`).

`commonallplayers` names every player NBA.com knows; a team roster adds birthdate, height and
position, and the draft slot when `HOW_ACQUIRED` still reads `#19 Pick in 2023 Draft` (a player
since traded or signed elsewhere has no draft slot here).

A roster is a snapshot, so its memberships are open-ended from the date it was taken; `derive`
closes and stitches them with transactions (NEU-1869). The contract type is read from
`HOW_ACQUIRED` where it names one (two-way, 10-day, Exhibit 10), and is `standard` otherwise.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from closinglineup.parse.rows import ContractType, ParseError, PlayerRow, RosterMembershipRow

type Payload = dict[str, Any]

ALL_PLAYERS = "CommonAllPlayers"
TEAM_ROSTER = "CommonTeamRoster"
ROSTER_SOURCE = "commonteamroster"

_BIRTH_DATE_FORMAT = "%b %d, %Y"
_HEIGHT = re.compile(r"^(\d+)-(\d+)$")
_DRAFTED = re.compile(r"^#(\d+) Pick in (\d{4}) Draft$")
# Compared lower-cased against `HOW_ACQUIRED`.
_CONTRACT_MARKERS: tuple[tuple[re.Pattern[str], ContractType], ...] = (
    (re.compile(r"two[- ]way"), "two_way"),
    (re.compile(r"(10|ten)[- ]day"), "ten_day"),
    (re.compile(r"exhibit[- ]10"), "exhibit_10"),
)


def result_set(payload: Payload, name: str) -> list[Payload]:
    """A stats.nba.com `resultSets` entry as one dict per row, keyed by its headers."""
    for found in payload.get("resultSets", []):
        if found["name"] == name:
            headers: list[str] = found["headers"]
            return [dict(zip(headers, row, strict=True)) for row in found["rowSet"]]
    raise ParseError(f"payload has no {name} result set")


def _birthdate(value: str | None) -> date | None:
    return datetime.strptime(value, _BIRTH_DATE_FORMAT).date() if value else None


def _height_in(value: str | None) -> int | None:
    found = _HEIGHT.match(value or "")
    return int(found[1]) * 12 + int(found[2]) if found else None


def contract_type(how_acquired: str | None) -> ContractType:
    lowered = (how_acquired or "").lower()
    for marker, kind in _CONTRACT_MARKERS:
        if marker.search(lowered):
            return kind
    return "standard"


def parse_players(all_players: Payload, rosters: Iterable[Payload] = ()) -> list[PlayerRow]:
    """A row per player in `commonallplayers` or any of `rosters`, filled in from the rosters."""
    names: dict[int, str] = {
        int(row["PERSON_ID"]): row["DISPLAY_FIRST_LAST"]
        for row in result_set(all_players, ALL_PLAYERS)
    }
    details: dict[int, Payload] = {
        int(row["PLAYER_ID"]): row for roster in rosters for row in result_set(roster, TEAM_ROSTER)
    }
    rows: list[PlayerRow] = []
    for player_id in names.keys() | details.keys():
        detail = details.get(player_id, {})
        drafted = _DRAFTED.match(detail.get("HOW_ACQUIRED") or "")
        rows.append(
            PlayerRow.model_validate(
                dict(
                    nba_player_id=player_id,
                    name=names.get(player_id) or detail["PLAYER"],
                    birthdate=_birthdate(detail.get("BIRTH_DATE")),
                    draft_year=int(drafted[2]) if drafted else None,
                    draft_pick=int(drafted[1]) if drafted else None,
                    height_in=_height_in(detail.get("HEIGHT")),
                    position=detail.get("POSITION") or None,
                )
            )
        )
    return sorted(rows, key=lambda row: row.nba_player_id)


def parse_roster(roster: Payload, as_of: date) -> list[RosterMembershipRow]:
    """A membership per player on the roster, open from `as_of`, the day it was fetched."""
    return [
        RosterMembershipRow.model_validate(
            dict(
                player_id=row["PLAYER_ID"],
                team_id=row["TeamID"],
                start_date=as_of,
                contract_type=contract_type(row.get("HOW_ACQUIRED")),
                source=ROSTER_SOURCE,
            )
        )
        for row in result_set(roster, TEAM_ROSTER)
    ]

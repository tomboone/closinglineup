"""NBA.com's player movement: signings, waivers, trades and conversions (spec §4.4).

`NBA_Player_Movement.json` is one file of every move since 2015-07-01, newest first, which
stats.nba.com serves outside its `/stats` API, so it is fetched through the same `NbaSession`.
`fetch` keeps it in the raw cache as `raw/nba_stats/playermovement/<date fetched>.json`, so the
`transactions` overlay re-run on the same day reads the same file.

Each row has a `Transaction_Type`, an English description, the team the move is for (`TEAM_ID`)
and the player (`PLAYER_ID`, 0 for a trade of draft consideration or cash). The feed fetched on
2026-10-23 (10,118 rows) holds five types, which `classify` maps:

| `Transaction_Type` | Description | Type |
|---|---|---|
| `Signing` | "… signed guard X to a Two-Way Contract." | `two_way_sign` |
| `Signing` | any other contract: "… re-signed X to a Veteran Extension." | `sign` |
| `AwardOnWaivers` | "… claimed forward X off waivers." | `sign` |
| `Waive` | "… waived forward X." | `waive` |
| `Trade` | "Chicago Bulls received guard X from Charlotte Hornets." | `trade` |
| `ContractConverted` | "… converted the contract of X to a Two-Way Contract." | `two_way_convert` |

A conversion the other way, "… to an NBA Contract.", is a `two_way_convert` too.

The feed has no G League assignments or recalls and no option decisions: none of its 10,118 rows
mentions "assign", "recall", "G League" or "option". A row of any other `Transaction_Type` is
classified from its description (`assigned` → `assign`, `recalled` → `recall`, an option
`exercised` or `declined`), which is asserted, not observed, and is `other` otherwise.

A trade row is the receiving team's, so `TEAM_ID` is `to_team_id` and the team it came from is
read from the description. A waiver's team is `from_team_id`, an assignment's too; every other
row's team is `to_team_id`.
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, fetch_json
from closinglineup.teams import UnknownTeam, by_name, by_nba_team_id

SOURCE = "nba_stats"
ENDPOINT = "playermovement"
URL = "https://stats.nba.com/js/data/playermovement/NBA_Player_Movement.json"
ROOT = "NBA_Player_Movement"

type TransactionType = Literal[
    "sign",
    "waive",
    "trade",
    "two_way_sign",
    "two_way_convert",
    "assign",
    "recall",
    "option_exercise",
    "option_decline",
    "other",
]
type Payload = dict[str, Any]

_BY_TRANSACTION_TYPE: dict[str, TransactionType] = {
    "Signing": "sign",
    "AwardOnWaivers": "sign",
    "Waive": "waive",
    "Trade": "trade",
    "ContractConverted": "two_way_convert",
}
_TWO_WAY = re.compile(r"two-way contract", re.IGNORECASE)
# Asserted, not observed: the feed of 2026-10-23 has no row any of these match.
_BY_DESCRIPTION: tuple[tuple[re.Pattern[str], TransactionType], ...] = (
    (re.compile(r"\bassigned\b", re.IGNORECASE), "assign"),
    (re.compile(r"\brecalled\b", re.IGNORECASE), "recall"),
    (re.compile(r"\bexercised\b.*\boption\b", re.IGNORECASE), "option_exercise"),
    (re.compile(r"\bdeclined\b.*\boption\b", re.IGNORECASE), "option_decline"),
)
# A trade's description ends with the team the player came from.
_TRADED_FROM = re.compile(r" from (.+?)\.?$")
# The types whose row's team is the one the player leaves.
_FROM_TEAM_TYPES: frozenset[TransactionType] = frozenset({"waive", "assign"})


@dataclass(frozen=True, slots=True)
class TransactionRow:
    """One move, as a `transaction` row's column values."""

    date: date
    type: TransactionType
    player_id: int
    from_team_id: int | None
    to_team_id: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class PlayerMovement:
    rows: list[TransactionRow]
    # Rows that name no player: a trade of draft consideration or cash.
    no_player: int
    # Rows repeating an earlier row's player, date, type and description, which have no row.
    duplicates: int
    # Each row whose team is not an NBA team, as its description; it has no row.
    unknown_teams: list[str]


def fetch(session: NbaSession, cache: RawCache, day: date, *, force: bool = False) -> Payload:
    """The feed, cached under `day`, the date it is fetched for; a cached copy is not re-fetched
    unless `force`. A response with no rows raises `NbaHttpError` rather than being cached."""

    def check(data: Payload) -> None:
        if not data.get(ROOT, {}).get("rows"):
            raise NbaHttpError(f"{URL} has no rows")

    return fetch_json(
        session, cache, SOURCE, ENDPOINT, day.isoformat(), URL, force=force, check=check
    )


def classify(transaction_type: str, description: str) -> TransactionType:
    """A row's type, from its `Transaction_Type` where the feed's five name one, else from its
    description."""
    found = _BY_TRANSACTION_TYPE.get(transaction_type)
    if found == "sign" and _TWO_WAY.search(description):
        return "two_way_sign"
    if found is not None:
        return found
    for pattern, kind in _BY_DESCRIPTION:
        if pattern.search(description):
            return kind
    return "other"


def parse(payload: Payload) -> PlayerMovement:
    """Every row of the feed that names a player and an NBA team, in the feed's order."""
    rows: list[TransactionRow] = []
    unknown_teams: list[str] = []
    no_player = 0
    seen: Counter[tuple[int, date, TransactionType, str]] = Counter()
    for raw in payload[ROOT]["rows"]:
        description: str = raw["TRANSACTION_DESCRIPTION"]
        player_id = int(raw["PLAYER_ID"] or 0)
        if not player_id:
            no_player += 1
            continue
        kind = classify(raw["Transaction_Type"], description)
        try:
            team_id = by_nba_team_id(int(raw["TEAM_ID"])).nba_team_id
        except (TypeError, ValueError, UnknownTeam):
            unknown_teams.append(description)
            continue
        day = datetime.fromisoformat(raw["TRANSACTION_DATE"]).date()
        seen[(player_id, day, kind, description)] += 1
        if seen[(player_id, day, kind, description)] > 1:
            continue
        from_team_id = team_id if kind in _FROM_TEAM_TYPES else None
        to_team_id = None if kind in _FROM_TEAM_TYPES else team_id
        if kind == "trade":
            from_team_id = _traded_from(description)
        rows.append(TransactionRow(day, kind, player_id, from_team_id, to_team_id, description))
    duplicates = sum(count - 1 for count in seen.values())
    return PlayerMovement(rows, no_player, duplicates, unknown_teams)


def _traded_from(description: str) -> int | None:
    found = _TRADED_FROM.search(description)
    if found is None:
        return None
    try:
        return by_name(found[1]).nba_team_id
    except UnknownTeam:
        return None

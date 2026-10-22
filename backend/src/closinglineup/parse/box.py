"""One game's stats.nba.com box scores → a `PlayerGameRow` per player (spec §4.1, §6.2
`player_game`).

`boxscoretraditionalv3` is the spine: every player it lists gets a row, DNPs included, and
supplies starters, minutes, the counting stats and the DNP comment. `boxscoreadvancedv3`
supplies the rates and ratings, `boxscoreusagev3` the usage rate, and `boxscorehustlev2` the
deflections, contests and charges; any of the three may be missing for a game, leaving its
columns NULL. A DNP's advanced and hustle columns are NULL too: advanced reports them as zeros
and hustle leaves the player out.

The traditional box lists only the active players, so an inactive player has no row here.

In the v3 boxes a starter is the player with a `position`; bench players have an empty one.
Minutes are `MM:SS`, rounded per player so that the team's sum comes out exact, which gives
`28:60` for a player cdn.nba.com has at `PT28M59.70S`. That form parses too, rounded to the
second.
"""

import re
from typing import Any

from closinglineup.parse.rows import DnpReason, ParseError, PlayerGameRow

type Payload = dict[str, Any]

TRADITIONAL_ROOT = "boxScoreTraditional"
ADVANCED_ROOT = "boxScoreAdvanced"
HUSTLE_ROOT = "boxScoreHustle"
USAGE_ROOT = "boxScoreUsage"
SIDES = ("homeTeam", "awayTeam")
SOURCE = "stats"

_CLOCK = re.compile(r"^(\d+):(\d+)$")
_ISO = re.compile(r"^PT(\d+)M(\d+(?:\.\d+)?)S$")

# A DNP comment's reason, by the first marker it contains (compared lower-cased). Order matters:
# "NWT - G League - Two-Way" is a G League DNP, not a not-with-team one. The fixture game has
# only "DNP - Coach's Decision"; the others follow NBA.com's DNP/DND/NWT comment forms, such as
# "DND - Injury/Illness", "NWT - Personal Reasons" and "DNP - G League - On Assignment".
_DNP_MARKERS: tuple[tuple[str, DnpReason], ...] = (
    ("g league", "g_league"),
    ("suspen", "suspension"),
    ("injury", "injury"),
    ("illness", "injury"),
    ("coach", "cd"),
    ("nwt", "inactive"),
    ("not with team", "inactive"),
    ("inactive", "inactive"),
    ("personal", "inactive"),
)


def parse_seconds(minutes: str | None) -> int:
    """Seconds played from `MM:SS` or `PTmmMss.ssS`; an empty or missing value is 0."""
    if not minutes:
        return 0
    if found := _CLOCK.match(minutes):
        return int(found[1]) * 60 + int(found[2])
    if found := _ISO.match(minutes):
        return round(int(found[1]) * 60 + float(found[2]))
    raise ParseError(f"unreadable minutes {minutes!r}")


def classify_dnp(comment: str) -> DnpReason:
    lowered = comment.lower()
    for marker, reason in _DNP_MARKERS:
        if marker in lowered:
            return reason
    return "other"


def _box(payload: Payload, root: str, game_id: str | None = None) -> Payload:
    try:
        box: Payload = payload[root]
    except KeyError:
        raise ParseError(f"payload has no {root}") from None
    if game_id is not None and box.get("gameId") != game_id:
        raise ParseError(f"{root} is for game {box.get('gameId')}, not {game_id}")
    return box


def _stats_by_player(payload: Payload | None, root: str, game_id: str) -> dict[int, Payload]:
    if payload is None:
        return {}
    box = _box(payload, root, game_id)
    return {
        int(player["personId"]): player.get("statistics") or {}
        for side in SIDES
        for player in box[side]["players"]
    }


def parse_box(
    traditional: Payload,
    advanced: Payload | None = None,
    hustle: Payload | None = None,
    usage: Payload | None = None,
) -> list[PlayerGameRow]:
    """A row per player in the traditional box, home team first."""
    box = _box(traditional, TRADITIONAL_ROOT)
    game_id: str = box["gameId"]
    advanced_by = _stats_by_player(advanced, ADVANCED_ROOT, game_id)
    hustle_by = _stats_by_player(hustle, HUSTLE_ROOT, game_id)
    usage_by = _stats_by_player(usage, USAGE_ROOT, game_id)

    rows: list[PlayerGameRow] = []
    for side in SIDES:
        team = box[side]
        for player in team["players"]:
            player_id = int(player["personId"])
            stats: Payload = player.get("statistics") or {}
            seconds = parse_seconds(stats.get("minutes"))
            played = seconds > 0
            adv = advanced_by.get(player_id, {}) if played else {}
            hus = hustle_by.get(player_id, {}) if played else {}
            use = usage_by.get(player_id, {}) if played else {}
            rows.append(
                PlayerGameRow.model_validate(
                    dict(
                        game_id=game_id,
                        player_id=player_id,
                        team_id=team["teamId"],
                        is_starter=bool(player.get("position")),
                        seconds=seconds,
                        dnp_reason=None if played else classify_dnp(player.get("comment") or ""),
                        pts=stats.get("points"),
                        fgm=stats.get("fieldGoalsMade"),
                        fga=stats.get("fieldGoalsAttempted"),
                        fg3m=stats.get("threePointersMade"),
                        fg3a=stats.get("threePointersAttempted"),
                        ftm=stats.get("freeThrowsMade"),
                        fta=stats.get("freeThrowsAttempted"),
                        oreb=stats.get("reboundsOffensive"),
                        dreb=stats.get("reboundsDefensive"),
                        ast=stats.get("assists"),
                        stl=stats.get("steals"),
                        blk=stats.get("blocks"),
                        tov=stats.get("turnovers"),
                        pf=stats.get("foulsPersonal"),
                        plus_minus=stats.get("plusMinusPoints"),
                        ts_pct=adv.get("trueShootingPercentage"),
                        efg_pct=adv.get("effectiveFieldGoalPercentage"),
                        usg_pct=use.get("usagePercentage"),
                        ast_pct=adv.get("assistPercentage"),
                        tov_pct=adv.get("turnoverRatio"),
                        oreb_pct=adv.get("offensiveReboundPercentage"),
                        dreb_pct=adv.get("defensiveReboundPercentage"),
                        off_rating=adv.get("offensiveRating"),
                        def_rating=adv.get("defensiveRating"),
                        pace=adv.get("pace"),
                        deflections=hus.get("deflections"),
                        contested_shots=hus.get("contestedShots"),
                        charges_drawn=hus.get("chargesDrawn"),
                        source=SOURCE,
                    )
                )
            )
    return rows

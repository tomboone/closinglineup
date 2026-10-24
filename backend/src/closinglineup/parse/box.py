"""One game's box scores → a `PlayerGameRow` per player (spec §4.1, §4.2, §6.2 `player_game`):
stats.nba.com's (`parse_box`), or cdn.nba.com's live box when stats.nba.com failed for the game
(`parse_cdn_box`).

`boxscoretraditionalv3` is the spine: every player it lists gets a row, DNPs included, and
supplies starters, minutes, the counting stats and the DNP comment. `boxscoreadvancedv3`
supplies the rates and ratings, `boxscoreusagev3` the usage rate, and `boxscorehustlev2` the
deflections, contests and charges; any of the three may be missing for a game, leaving its
columns NULL. A DNP's advanced and hustle columns are NULL too: advanced reports them as zeros
and hustle leaves the player out. Rates are fractions (0.222 for 22.2%); advanced's
`turnoverRatio` is turnovers per 100 plays, so it is divided by 100 to match.

The traditional box lists only the active players, so an inactive player has no row here, and no
stats.nba.com box this fetcher pulls lists them; cdn.nba.com's live box does.

In the v3 boxes a starter is the player with a `position`; bench players have an empty one.
Minutes are `MM:SS`, rounded per player so that the team's sum comes out exact, which gives
`28:60` for a player cdn.nba.com has at `PT28M59.70S`. That form parses too, rounded to the
second.

The live box (`parse_cdn_box`) has the traditional box's counting stats under the same names, and
no rates, ratings or hustle numbers, so those columns are NULL and the row's `source` is `cdn`. It
lists the inactive players too (`status` `INACTIVE`), which are left out, so its rows are the
stats.nba.com box's players: a stats.nba.com re-fetch then replaces each of them. A starter has
`starter` `"1"`. A DNP gives its reason as a code, `DND_INJURY` say, or none at all when it was the
coach's decision. The live box's minutes for the stats.nba.com fixture game round to the same
second as its traditional box's for every player.
"""

import re
from typing import Any

from closinglineup.parse.rows import DnpReason, ParseError, PlayerGameRow

type Payload = dict[str, Any]

TRADITIONAL_ROOT = "boxScoreTraditional"
ADVANCED_ROOT = "boxScoreAdvanced"
HUSTLE_ROOT = "boxScoreHustle"
USAGE_ROOT = "boxScoreUsage"
CDN_ROOT = "game"
SIDES = ("homeTeam", "awayTeam")
SOURCE = "stats"
CDN_SOURCE = "cdn"
# The live box's `status` of a player who dressed; the others are `INACTIVE`.
ACTIVE = "ACTIVE"

_CLOCK = re.compile(r"^(\d+):(\d+)$")
_ISO = re.compile(r"^PT(\d+)M(\d+(?:\.\d+)?)S$")

# A DNP comment's reason, by the first marker it contains (compared lower-cased). Order matters:
# "NWT - G League - Two-Way" is a G League DNP, not a not-with-team one. The fixture game has
# only "DNP - Coach's Decision"; the others follow NBA.com's DNP/DND/NWT comment forms, such as
# "DND - Injury/Illness", "NWT - Personal Reasons" and "DNP - G League - On Assignment", and the
# live box's reason codes, such as `DND_INJURY` and `INACTIVE_GLEAGUE_ON_ASSIGNMENT`.
_DNP_MARKERS: tuple[tuple[str, DnpReason], ...] = (
    ("g league", "g_league"),
    ("gleague", "g_league"),
    ("suspen", "suspension"),
    ("injury", "injury"),
    ("illness", "injury"),
    ("coach", "cd"),
    ("nwt", "inactive"),
    ("not with team", "inactive"),
    ("inactive", "inactive"),
    ("personal", "inactive"),
)


# Each counting-stat column, by the name both boxes give it.
_COUNTS = {
    "pts": "points",
    "fgm": "fieldGoalsMade",
    "fga": "fieldGoalsAttempted",
    "fg3m": "threePointersMade",
    "fg3a": "threePointersAttempted",
    "ftm": "freeThrowsMade",
    "fta": "freeThrowsAttempted",
    "oreb": "reboundsOffensive",
    "dreb": "reboundsDefensive",
    "ast": "assists",
    "stl": "steals",
    "blk": "blocks",
    "tov": "turnovers",
    "pf": "foulsPersonal",
    "plus_minus": "plusMinusPoints",
}


def parse_seconds(minutes: str | None) -> int:
    """Seconds played from `MM:SS` or `PTmmMss.ssS`; an empty or missing value is 0."""
    if not minutes:
        return 0
    if found := _CLOCK.match(minutes):
        return int(found[1]) * 60 + int(found[2])
    if found := _ISO.match(minutes):
        return round(int(found[1]) * 60 + float(found[2]))
    raise ParseError(f"unreadable minutes {minutes!r}")


def _per_100(value: float | str | None) -> float | None:
    return None if value is None else float(value) / 100


def classify_dnp(comment: str) -> DnpReason:
    lowered = comment.lower()
    for marker, reason in _DNP_MARKERS:
        if marker in lowered:
            return reason
    return "other"


def _counts(stats: Payload) -> Payload:
    """The counting stats, which both boxes name alike; a missing one is left out, to fail
    validation rather than read as zero."""
    return {column: stats[name] for column, name in _COUNTS.items() if name in stats}


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
                        **_counts(stats),
                        ts_pct=adv.get("trueShootingPercentage"),
                        efg_pct=adv.get("effectiveFieldGoalPercentage"),
                        usg_pct=use.get("usagePercentage"),
                        ast_pct=adv.get("assistPercentage"),
                        tov_pct=_per_100(adv.get("turnoverRatio")),
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


def parse_cdn_box(live: Payload) -> list[PlayerGameRow]:
    """A row per active player in cdn.nba.com's live box, home team first."""
    box = _box(live, CDN_ROOT)
    rows: list[PlayerGameRow] = []
    for side in SIDES:
        team = box[side]
        for player in team["players"]:
            if player.get("status") != ACTIVE:
                continue
            stats: Payload = player.get("statistics") or {}
            seconds = parse_seconds(stats.get("minutes"))
            reason: str | None = player.get("notPlayingReason")
            dnp_reason = None if seconds > 0 else classify_dnp(reason) if reason else "cd"
            rows.append(
                PlayerGameRow.model_validate(
                    dict(
                        game_id=box["gameId"],
                        player_id=int(player["personId"]),
                        team_id=team["teamId"],
                        is_starter=player.get("starter") == "1",
                        seconds=seconds,
                        dnp_reason=dnp_reason,
                        **_counts(stats),
                        source=CDN_SOURCE,
                    )
                )
            )
    return rows

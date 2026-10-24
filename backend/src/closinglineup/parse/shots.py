"""`shotchartdetail` → a `ShotRow` per field-goal attempt (spec §4.1, §6.2 `shot`).

The zone comes from NBA.com's own zones: `SHOT_ZONE_BASIC` names the restricted area, the paint,
mid-range, each corner three and the above-the-break three, and `SHOT_ZONE_RANGE` splits
mid-range at 16 feet, which `SHOT_ZONE_AREA` (the side of the floor) cannot. A paint shot outside
the restricted area is a short mid-range shot, and a backcourt heave an above-the-break three.
`assisted_by` is left NULL: the shot chart does not say, and the play-by-play may fill it later.
"""

from typing import Any

from closinglineup.parse.roster import result_set
from closinglineup.parse.rows import ParseError, ShotRow, ShotZone

SHOT_CHART = "Shot_Chart_Detail"

MID_RANGE = "Mid-Range"
_ZONES: dict[str, ShotZone] = {
    "Restricted Area": "rim",
    "In The Paint (Non-RA)": "short_mid",
    "Left Corner 3": "corner_3",
    "Right Corner 3": "corner_3",
    "Above the Break 3": "above_break_3",
    "Backcourt": "above_break_3",
}
# The `SHOT_ZONE_RANGE`s of a short mid-range shot; mid-range's others are 16 feet and out.
_SHORT_RANGES = frozenset({"Less Than 8 ft.", "8-16 ft."})


def shot_zone(basic: str, distance_range: str) -> ShotZone:
    """The zone of a shot NBA.com places in `basic` (`SHOT_ZONE_BASIC`) and `distance_range`
    (`SHOT_ZONE_RANGE`)."""
    if basic == MID_RANGE:
        return "short_mid" if distance_range in _SHORT_RANGES else "long_mid"
    try:
        return _ZONES[basic]
    except KeyError:
        raise ParseError(f"unknown shot zone {basic!r}") from None


def parse_shots(payload: dict[str, Any]) -> list[ShotRow]:
    return [
        ShotRow.model_validate(
            dict(
                game_id=shot["GAME_ID"],
                event_num=shot["GAME_EVENT_ID"],
                player_id=shot["PLAYER_ID"],
                team_id=shot["TEAM_ID"],
                period=shot["PERIOD"],
                clock_sec=shot["MINUTES_REMAINING"] * 60 + shot["SECONDS_REMAINING"],
                x=shot["LOC_X"],
                y=shot["LOC_Y"],
                distance_ft=shot["SHOT_DISTANCE"],
                zone=shot_zone(shot["SHOT_ZONE_BASIC"], shot["SHOT_ZONE_RANGE"]),
                shot_type=shot["ACTION_TYPE"],
                made=shot["SHOT_MADE_FLAG"],
            )
        )
        for shot in result_set(payload, SHOT_CHART)
    ]

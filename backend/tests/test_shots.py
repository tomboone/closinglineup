import json
from typing import Any

import pytest

from closinglineup.parse.box import parse_box
from closinglineup.parse.rows import ParseError, ShotZone
from closinglineup.parse.shots import parse_shots, shot_zone
from closinglineup.sources import nba_stats
from tests.test_nba_stats import fixture_body, season_fixture_body

# The shot chart fixture holds Stephen Curry's shots in the box score fixture game.
CURRY = 201939
GAME = "0022500006"
ZONES = {"rim", "short_mid", "long_mid", "corner_3", "above_break_3"}


def _shot_chart() -> dict[str, Any]:
    return json.loads(season_fixture_body(nba_stats.SHOT_CHART_DETAIL))


def test_every_shot_is_in_one_of_the_five_zones() -> None:
    shots = parse_shots(_shot_chart())

    assert len(shots) == 25
    assert {shot.zone for shot in shots} <= ZONES
    assert {(shot.game_id, shot.player_id) for shot in shots} == {(GAME, CURRY)}


def test_shots_made_and_attempted_match_the_box_score() -> None:
    box = parse_box(json.loads(fixture_body(nba_stats.BOXSCORE_TRADITIONAL_V3)), None, None, None)
    [curry] = [row for row in box if row.player_id == CURRY]
    shots = parse_shots(_shot_chart())

    assert len(shots) == curry.fga
    assert sum(shot.made for shot in shots) == curry.fgm
    threes = [shot for shot in shots if shot.zone in {"corner_3", "above_break_3"}]
    assert (len(threes), sum(shot.made for shot in threes)) == (curry.fg3a, curry.fg3m)


def test_a_shot_row_carries_its_event_clock_and_place() -> None:
    chart = _shot_chart()["resultSets"][0]
    shot = dict(zip(chart["headers"], chart["rowSet"][0], strict=True))

    first = parse_shots(_shot_chart())[0]

    assert first.event_num == shot["GAME_EVENT_ID"]
    assert first.clock_sec == shot["MINUTES_REMAINING"] * 60 + shot["SECONDS_REMAINING"]
    assert (first.x, first.y, first.distance_ft) == (
        shot["LOC_X"],
        shot["LOC_Y"],
        shot["SHOT_DISTANCE"],
    )
    assert first.shot_type == shot["ACTION_TYPE"]
    assert first.made is (shot["SHOT_MADE_FLAG"] == 1)
    assert (first.assisted_by, first.shot_clock_bucket) == (None, None)


@pytest.mark.parametrize(
    ("basic", "distance_range", "zone"),
    [
        ("Restricted Area", "Less Than 8 ft.", "rim"),
        ("In The Paint (Non-RA)", "Less Than 8 ft.", "short_mid"),
        ("In The Paint (Non-RA)", "8-16 ft.", "short_mid"),
        ("Mid-Range", "8-16 ft.", "short_mid"),
        ("Mid-Range", "16-24 ft.", "long_mid"),
        ("Left Corner 3", "24+ ft.", "corner_3"),
        ("Right Corner 3", "24+ ft.", "corner_3"),
        ("Above the Break 3", "24+ ft.", "above_break_3"),
        ("Backcourt", "Back Court Shot", "above_break_3"),
    ],
)
def test_a_shots_zone_comes_from_nba_coms_zones(
    basic: str, distance_range: str, zone: ShotZone
) -> None:
    assert shot_zone(basic, distance_range) == zone


def test_an_unknown_zone_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="unknown shot zone 'Half Court'"):
        shot_zone("Half Court", "24+ ft.")

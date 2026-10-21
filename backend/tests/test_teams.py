from collections import Counter

import pytest

from closinglineup.teams import TEAMS, UnknownTeam, by_espn_id, by_nba_team_id, by_tricode


def test_table_has_30_teams_unique_on_every_key() -> None:
    assert len(TEAMS) == 30
    assert len({t.nba_team_id for t in TEAMS}) == 30
    assert len({t.tricode for t in TEAMS}) == 30
    assert len({t.espn_id for t in TEAMS}) == 30
    assert len({t.name for t in TEAMS}) == 30


def test_15_teams_per_conference_and_5_per_division() -> None:
    assert Counter(t.conference for t in TEAMS) == {"East": 15, "West": 15}
    assert set(Counter(t.division for t in TEAMS).values()) == {5}
    assert {(t.conference, t.division) for t in TEAMS} == {
        ("East", "Atlantic"),
        ("East", "Central"),
        ("East", "Southeast"),
        ("West", "Northwest"),
        ("West", "Pacific"),
        ("West", "Southwest"),
    }


def test_tricode_resolves_to_nba_team_id() -> None:
    assert by_tricode("BOS").nba_team_id == 1610612738


def test_every_lookup_finds_the_same_row() -> None:
    for team in TEAMS:
        assert by_nba_team_id(team.nba_team_id) is team
        assert by_tricode(team.tricode) is team
        assert by_espn_id(team.espn_id) is team


def test_unknown_keys_raise() -> None:
    # 50014 is Hapoel Jerusalem, a preseason opponent in NBA.com's 2025-26 schedule.
    with pytest.raises(UnknownTeam):
        by_nba_team_id(50014)
    with pytest.raises(UnknownTeam):
        by_tricode("NY")
    with pytest.raises(UnknownTeam):
        by_espn_id(99)

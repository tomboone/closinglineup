import csv
import math
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, ImpactSnapshot, PlayerGame, RosterMembership
from closinglineup.derive import value
from closinglineup.derive.bpm import PlayerLine, TeamBox, TeamLine, compute, season_lines
from closinglineup.derive.value import (
    LeagueContext,
    PlayerSeason,
    StintWinShares,
    available_seconds,
    compute_value,
    league_context,
    player_seasons,
    season_win_shares,
    vorp,
    vorp_rows,
    win_shares,
    ws_rows,
)
from closinglineup.impact import ImpactSnapshotRow
from tests.test_bpm import FIXTURES, bref_season

# Every 2025-26 player's published VORP, read on 2026-10-27 from
# https://www.basketball-reference.com/leagues/NBA_2026_advanced.html, one row per row of
# `2025-26_players.csv` (`fixtures/bref/README.md`).
PUBLISHED_VORP = FIXTURES / "2025-26_vorp.csv"
# Every 2025-26 player's published OWS, DWS and WS, read on 2026-10-27 from the same page, one row
# per row of `2025-26_players.csv` (`fixtures/bref/README.md`).
PUBLISHED_WS = FIXTURES / "2025-26_ws.csv"


def test_every_regulars_vorp_is_within_the_published_value() -> None:
    with (FIXTURES / "2025-26_teams.csv").open() as handle:
        teams = {row["team"]: row for row in csv.DictReader(handle)}
    with (FIXTURES / "2025-26_players.csv").open() as handle:
        players = list(csv.DictReader(handle))
    with PUBLISHED_VORP.open() as handle:
        published = list(csv.DictReader(handle))

    regulars = 0
    for player, row in zip(players, published, strict=True):
        assert (row["bref_id"], row["team"]) == (player["bref_id"], player["team"])
        if int(player["mp"]) < 1000:
            continue
        regulars += 1
        team = teams[player["team"]]
        # A row is one team's: his minutes there over its players' together, over its season.
        season = PlayerSeason(0, int(player["mp"]) * 60, int(team["mp"]) * 60, int(team["g"]))
        # From the published BPM, so the check is of VORP's formula; local BPM's own error is
        # `tests/test_bpm.py`'s. The spec's tolerance is ±0.3; the published values are rounded to
        # a tenth, as are the BPMs they rest on, so the formula lands within 0.1.
        assert vorp(float(player["bpm"]), season) == pytest.approx(float(row["vorp"]), abs=0.1), (
            player["player"]
        )
    assert regulars == 263


def test_vorp_from_local_bpm_is_within_the_published_value_but_where_bpm_strays() -> None:
    """VORP over the BPM `derive.bpm` computes from the same fixture: local BPM's own error, up
    to a point (`tests/test_bpm.py`), carries through scaled by playing time. Two regulars land
    just outside the spec's ±0.3, each from a local BPM 0.5 to 0.9 below the published one."""
    lines, teams, published = bref_season()
    keys = list(published)
    with PUBLISHED_VORP.open() as handle:
        vorps = {
            f"{row['bref_id']}/{row['team']}": float(row["vorp"]) for row in csv.DictReader(handle)
        }

    outside: list[str] = []
    for result in compute(lines, teams):
        if result.minutes < 1000:
            continue
        team = teams[result.team_id]
        season = PlayerSeason(0, round(result.minutes * 60), round(team.minutes * 300), team.games)
        key = keys[result.player_id - 1]
        error = abs(vorp(result.bpm, season) - vorps[key])
        assert error < 0.35, key
        if error > 0.3:
            outside.append(published[key]["player"])
    assert sorted(outside) == ["Cade Cunningham", "Jaren Jackson Jr."]


def test_every_regulars_win_shares_are_within_the_published_value() -> None:
    lines, teams, published = bref_season()
    keys = list(published)
    with PUBLISHED_WS.open() as handle:
        rows = list(csv.DictReader(handle))
    assert [f"{row['bref_id']}/{row['team']}" for row in rows] == keys

    regulars = 0
    for stint in season_win_shares(lines, teams):
        if stint.minutes < 1000:
            continue
        regulars += 1
        row = rows[stint.player_id - 1]
        name = published[keys[stint.player_id - 1]]["player"]
        # The spec's tolerance is ±0.5 of each; the published values are rounded to a tenth, and
        # the formulas over the published totals land within 0.1 of all three.
        assert stint.ows == pytest.approx(float(row["ows"]), abs=0.1), name
        assert stint.dws == pytest.approx(float(row["dws"]), abs=0.1), name
        assert stint.ows + stint.dws == pytest.approx(float(row["ws"]), abs=0.1), name
    assert regulars == 263


def test_league_win_shares_total_within_three_percent_of_league_wins() -> None:
    lines, teams, _ = bref_season()

    total = sum(stint.ows + stint.dws for stint in season_win_shares(lines, teams))

    # 82 games a team, 30 teams, a win a game.
    assert total == pytest.approx(82 * 30 / 2, rel=0.03)


TEAM = 1
MIRROR = 2
# A team's season and its opponents', and a player on it; the league is the team and a mirror of
# it, which played its opponents' box against its own.
TEAM_BOX = TeamBox(
    fgm=3300, fga=7000, fg3m=1000, ftm=1400, fta=1800, orb=850, drb=2700,
    ast=2000, stl=650, blk=400, tov=1100, pf=1600, pts=9000,
)  # fmt: skip
OPP_BOX = TeamBox(
    fgm=3200, fga=7100, fg3m=0, ftm=1300, fta=1700, orb=900, drb=2650,
    ast=0, stl=0, blk=0, tov=1150, pf=0, pts=8800,
)  # fmt: skip
# Basketball Reference's estimate over the two boxes.
POSSESSIONS = 7872.6393
LINE = PlayerLine(
    7, TEAM, 2400.0, 3.0, pts=1420, fgm=500, fga=1050, fg3m=120, ftm=300, fta=380,
    orb=60, drb=300, ast=350, stl=80, blk=30, tov=160, pf=170,
)  # fmt: skip


def _team(team_id: int = TEAM, box: TeamBox = TEAM_BOX, opp: TeamBox = OPP_BOX) -> TeamLine:
    # 19800 player-minutes: 3960 of the clock.
    return TeamLine(team_id, 82, 3960.0, POSSESSIONS, box.pts, opp.pts, box, opp)


def _league() -> LeagueContext:
    league = league_context([_team(), _team(MIRROR, OPP_BOX, TEAM_BOX)])
    assert league is not None
    return league


LEAGUE = _league()


def test_win_shares_follow_the_published_method_by_hand() -> None:
    """Worked by hand from the published formulas, apart from the code: a marginal 34.7317 points
    a win; offense from qAST 0.5719, 637.736 scoring and 1211.5514 total possessions and
    1411.8549 points produced; defense from a Stop% of 0.4903 and a rating of 112.7701."""
    assert (LEAGUE.pts, LEAGUE.possessions, LEAGUE.team_games) == (17800, 2 * POSSESSIONS, 164)
    assert LEAGUE.pace == pytest.approx(95.4259, abs=1e-4)

    stint = win_shares(LINE, _team(), LEAGUE)

    assert stint is not None
    assert (stint.player_id, stint.team_id, stint.minutes) == (7, TEAM, 2400.0)
    assert stint.ows == pytest.approx(4.3698, abs=1e-4)
    assert stint.dws == pytest.approx(2.5617, abs=1e-4)


@pytest.mark.parametrize(
    "changes",
    [
        # No free throws: the free-throw terms drop out.
        {"ftm": 0, "fta": 0, "pts": 1120},
        # No shots: the field-goal parts drop out.
        {"fgm": 0, "fga": 0, "fg3m": 0, "pts": 300},
    ],
)
def test_a_players_zero_denominators_are_zero(changes: dict[str, int]) -> None:
    whole = win_shares(LINE, _team(), LEAGUE)

    stint = win_shares(replace(LINE, **changes), _team(), LEAGUE)

    # His points are gone with what they cost, so his offense is worse; his defense is untouched.
    assert whole is not None and stint is not None
    assert math.isfinite(stint.ows) and stint.ows < whole.ows
    assert stint.dws == whole.dws


def test_a_team_with_no_fouls_gives_no_foul_share() -> None:
    fouled = win_shares(LINE, _team(), LEAGUE)
    unfouled = win_shares(LINE, _team(box=TEAM_BOX._replace(pf=0)), LEAGUE)

    assert fouled is not None and unfouled is not None
    assert unfouled.ows == fouled.ows
    # The foul share's stops are gone, so his defensive rating is worse.
    assert unfouled.dws < fouled.dws


@pytest.mark.parametrize(
    "team",
    [
        TeamLine(TEAM, 1, 48.0, 0.0, 0, 0, TEAM_BOX, OPP_BOX),
        _team(box=TEAM_BOX._replace(fga=0)),
        _team(box=TEAM_BOX._replace(fta=0, ftm=0)),
        _team(opp=OPP_BOX._replace(fga=0, fgm=0)),
        _team(opp=OPP_BOX._replace(fta=0, ftm=0)),
    ],
)
def test_a_team_without_the_box_its_formulas_divide_by_gives_no_stint(team: TeamLine) -> None:
    assert win_shares(LINE, team, LEAGUE) is None


def test_a_stint_of_no_minutes_is_none() -> None:
    assert win_shares(replace(LINE, minutes=0.0), _team(), LEAGUE) is None


def test_a_league_without_possessions_has_no_context_or_stints() -> None:
    empty = TeamLine(TEAM, 0, 0.0, 0.0, 0, 0, OPP_BOX, OPP_BOX)

    assert league_context([empty]) is None
    assert season_win_shares([LINE], {TEAM: empty}) == []


def test_ws_rows_sum_a_traded_players_stints_and_drop_a_player_under_a_minute() -> None:
    day = date(2026, 10, 27)
    stints = [
        StintWinShares(8, 2, 0.5, 0.01, 0.01),
        StintWinShares(7, 2, 600.04, 1.23456, -0.5),
        StintWinShares(7, 1, 1200.0, 2.0, 1.11111),
    ]

    assert ws_rows(day, stints) == [
        ImpactSnapshotRow(day, 7, "ws_local", 3.846, 3.235, 0.611, 1800.0),
    ]


def test_vorp_is_bpm_over_replacement_times_the_share_of_the_season_played() -> None:
    # Half the floor time of 41 games: a quarter of a season's minutes.
    season = PlayerSeason(1, 41 * 1440, 41 * 14400, 41)

    assert season.minutes_share == pytest.approx(0.1)
    assert vorp(4.0, season) == pytest.approx(6.0 * 0.5 * 0.5)
    assert vorp(-2.0, season) == 0.0
    assert vorp(4.0, PlayerSeason(1, 0, 0, 0)) == 0.0


@pytest.mark.parametrize(
    ("seconds", "available"),
    [(0, 14400), (14400, 14400), (14398, 14400), (15900, 15900), (15890, 15900), (17400, 17400)],
)
def test_available_seconds_are_five_players_over_regulation_and_each_overtime(
    seconds: int, available: int
) -> None:
    assert available_seconds(seconds) == available


def test_vorp_rows_are_one_per_bpm_rounded_with_his_minutes_as_the_basis() -> None:
    day = date(2026, 10, 23)
    seasons = {7: PlayerSeason(7, 2 * 2400, 2 * 14400, 2)}

    assert vorp_rows(day, {8: 3.0, 7: 1.0}, seasons) == [
        ImpactSnapshotRow(
            day, 7, "vorp_local", round(3.0 * 5 * 4800 / 28800 * 2 / 82, 3), None, None, 80.0
        ),
        ImpactSnapshotRow(day, 8, "vorp_local", 0.0, None, None, 0.0),
    ]


GSW = 1610612744
DEN = 1610612743
LAL = 1610612747
DAY = date(2026, 10, 20)
# GSW-DEN on DAY, DEN-GSW the next day in overtime, DEN-LAL the day after; a playoff game and a
# scheduled one, neither counted.
FIRST = "0022600901"
OVERTIME = "0022600902"
THIRD = "0022600903"
PLAYOFF = "0042600901"
LATER = "0022600904"
GAMES = {
    FIRST: (DAY, "regular", "final", GSW, DEN),
    OVERTIME: (DAY + timedelta(days=1), "regular", "final", DEN, GSW),
    THIRD: (DAY + timedelta(days=2), "regular", "final", DEN, LAL),
    PLAYOFF: (DAY + timedelta(days=2), "playoff", "final", GSW, LAL),
    LATER: (DAY + timedelta(days=3), "regular", "scheduled", DEN, LAL),
}
THROUGH = DAY + timedelta(days=2)
# Traded from GSW to DEN after the first game: 30 minutes for GSW, then 20 and 10 for DEN.
TRADED = 930001
# On GSW all along with no box line but one game's 12 minutes.
BENCH = 930002
# DEN's, with no roster span, 40 minutes in the third game.
UNLISTED = 930003
# A stale VORP and a Win Shares row on THROUGH, for a player with no BPM there.
STALE = 930004
PLAYERS = (TRADED, BENCH, UNLISTED, STALE)
# Every line that played: 10 points on 4 of 9 (a three) and 1 of 2 free throws, 1 and 3
# boards, 2 assists, a steal, a turnover and 2 fouls. VORP reads only the minutes; Win Shares
# needs a box on both sides.
BOX = {
    **{"pts": 10, "fgm": 4, "fga": 9, "fg3m": 1, "fg3a": 3, "ftm": 1, "fta": 2},
    **{"oreb": 1, "dreb": 3, "ast": 2, "stl": 1, "blk": 0, "tov": 1, "pf": 2, "plus_minus": 0},
}


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(ImpactSnapshot).where(ImpactSnapshot.player_id.in_(PLAYERS)))
        session.execute(delete(RosterMembership).where(RosterMembership.player_id.in_(PLAYERS)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(list(GAMES))))
        session.execute(delete(Game).where(Game.game_id.in_(list(GAMES))))


def _player_game(game_id: str, player_id: int, team_id: int, seconds: int) -> PlayerGame:
    """A line of `seconds` and `BOX`."""
    return PlayerGame(
        game_id=game_id,
        player_id=player_id,
        team_id=team_id,
        is_starter=False,
        seconds=seconds,
        dnp_reason=None,
        **BOX,
        source="stats",
    )


def _span(player_id: int, team_id: int, start: date, end: date | None) -> RosterMembership:
    return RosterMembership(
        player_id=player_id,
        team_id=team_id,
        start_date=start,
        end_date=end,
        contract_type="standard",
        source="test",
    )


def _bpm(player_id: int, day: date, bpm: float, source: str = "bpm_local") -> ImpactSnapshot:
    return ImpactSnapshot(
        date=day, player_id=player_id, source=source, value=bpm, minutes_basis=None
    )


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """The games above. Each team's box is filler players' seconds making a regulation game, or
    one overtime in the second; TRADED, BENCH and UNLISTED take their minutes out of it. `_clear`
    removes the filler with the games."""
    _clear(db)
    with db.begin() as session:
        for game_id, (day, phase, status, home, away) in GAMES.items():
            session.add(
                Game(
                    game_id=game_id,
                    season="2026-27",
                    game_date=day,
                    phase=phase,
                    home_team_id=home,
                    away_team_id=away,
                    status=status,
                    home_b2b=False,
                    away_b2b=False,
                )
            )
        session.flush()
        played = {
            (FIRST, GSW): [(TRADED, 1800), (BENCH, 720)],
            (OVERTIME, DEN): [(TRADED, 1200)],
            (THIRD, DEN): [(TRADED, 600), (UNLISTED, 2400)],
        }
        for game_id, (_, _, status, home, away) in GAMES.items():
            if status != "final":
                continue
            length = 5 * (2880 + (300 if game_id == OVERTIME else 0))
            for team_id in (home, away):
                ours = played.get((game_id, team_id), [])
                for player_id, seconds in ours:
                    session.add(_player_game(game_id, player_id, team_id, seconds))
                rest = length - sum(seconds for _, seconds in ours)
                # The filler, five IDs a team, sharing the rest.
                for i in range(5):
                    filler = 940000 + team_id % 1000 * 10 + i
                    session.add(_player_game(game_id, filler, team_id, rest // 5))
        session.add_all(
            [
                _span(TRADED, GSW, date(2026, 10, 1), DAY),
                _span(TRADED, DEN, DAY + timedelta(days=1), None),
                _span(BENCH, GSW, date(2026, 10, 1), None),
                _bpm(TRADED, THROUGH, 4.0),
                _bpm(BENCH, THROUGH, -1.0),
                _bpm(UNLISTED, THROUGH, 0.5),
                _bpm(STALE, THROUGH, 9.9, "vorp_local"),
                _bpm(STALE, THROUGH, 1.2, "ws_local"),
            ]
        )
    yield db
    _clear(db)


def test_a_traded_players_season_counts_both_teams_games(
    seeded: sessionmaker[Session],
) -> None:
    with seeded() as session:
        seasons = player_seasons(session, "2026-27", THROUGH, [TRADED, BENCH, UNLISTED])

    # GSW's first game, then DEN's overtime game and its third: the playoff game is not counted.
    assert seasons[TRADED] == PlayerSeason(TRADED, 3600, 14400 + 15900 + 14400, 3)
    # Rostered for GSW's two regular-season games, with a line in one.
    assert seasons[BENCH] == PlayerSeason(BENCH, 720, 14400 + 15900, 2)
    # No span, so only the game he played.
    assert seasons[UNLISTED] == PlayerSeason(UNLISTED, 2400, 14400, 1)


type Stored = tuple[float, float | None, float | None, float | None]


def _stored(db: sessionmaker[Session], day: date) -> dict[tuple[str, int], Stored]:
    """`day`'s VORP and Win Shares rows of the players above: value, O, D and minutes basis."""
    with db() as session:
        found = session.scalars(
            select(ImpactSnapshot).where(
                ImpactSnapshot.date == day,
                ImpactSnapshot.player_id.in_(PLAYERS),
                ImpactSnapshot.source.in_(["vorp_local", "ws_local"]),
            )
        )
        return {
            (row.source, row.player_id): (row.value, row.o_value, row.d_value, row.minutes_basis)
            for row in found
        }


def test_compute_value_writes_each_bpm_players_vorp_and_replaces_the_dates_rows(
    seeded: sessionmaker[Session],
) -> None:
    first = compute_value(seeded, THROUGH)
    stored = _stored(seeded, THROUGH)
    again = compute_value(seeded, THROUGH)

    assert first["season"] == "2026-27"
    assert first["removed"] == 2
    assert again["changed"] == 0
    assert again["removed"] == 0
    assert set(stored) == {
        (source, player_id)
        for source in ("vorp_local", "ws_local")
        for player_id in (TRADED, BENCH, UNLISTED)
    }
    # (BPM + 2) * 5 * his minutes over his teams' * team games / 82, by hand.
    expected = {
        TRADED: (6.0 * 5 * 3600 / 44700 * 3 / 82, 60.0),
        BENCH: (1.0 * 5 * 720 / 30300 * 2 / 82, 12.0),
        UNLISTED: (2.5 * 5 * 2400 / 14400 * 1 / 82, 40.0),
    }
    for player_id, (vorp_value, minutes) in expected.items():
        stored_value, *rest = stored["vorp_local", player_id]
        assert stored_value == pytest.approx(vorp_value, abs=0.0005)
        assert rest == [None, None, minutes]
    assert _stored(seeded, THROUGH) == stored


def test_compute_value_writes_every_player_with_a_minutes_win_shares(
    seeded: sessionmaker[Session],
) -> None:
    result = compute_value(seeded, THROUGH)
    stored = _stored(seeded, THROUGH)
    with seeded() as session:
        lines, teams = season_lines(session, "2026-27", THROUGH)

    # The traded player's two stints, GSW's and DEN's, summed into one row of his 60 minutes.
    expected = {row.player_id: row for row in ws_rows(THROUGH, season_win_shares(lines, teams))}
    assert result["ws_players"] == len(expected) == len({line.player_id for line in lines})
    for player_id, minutes in ((TRADED, 60.0), (BENCH, 12.0), (UNLISTED, 40.0)):
        row = expected[player_id]
        assert stored["ws_local", player_id] == (row.value, row.o_value, row.d_value, minutes)
        assert row.o_value is not None and row.d_value is not None
        assert row.value == pytest.approx(row.o_value + row.d_value, abs=0.0015)
    # The stale row of a player with no line is gone.
    assert ("ws_local", STALE) not in stored


def test_the_sources_are_the_local_ones() -> None:
    assert (value.VORP_SOURCE, value.WS_SOURCE) == ("vorp_local", "ws_local")

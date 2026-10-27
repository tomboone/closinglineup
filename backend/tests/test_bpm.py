import csv
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, ImpactSnapshot, Player, PlayerGame
from closinglineup.derive import bpm
from closinglineup.derive.bpm import (
    PlayerBpm,
    PlayerLine,
    TeamBox,
    TeamLine,
    compute,
    listed_position,
    possessions,
    season_lines,
    shift_to_mean,
    snapshot_rows,
    write_bpm,
)
from closinglineup.impact import ImpactSnapshotRow

FIXTURES = Path(__file__).parent / "fixtures" / "bref"
# Basketball Reference's listed positions on the five-point scale, and as NBA.com lists them,
# which is the prior `season_lines` has (`listed_position`).
BREF_POSITIONS = {"PG": 1.0, "SG": 2.0, "SF": 3.0, "PF": 4.0, "C": 5.0}
NBA_POSITIONS = {"PG": "G", "SG": "G", "SF": "F", "PF": "F", "C": "C"}
BOX = ("fg", "fga", "fta", "orb", "drb", "tov")

# The 2016-17 Cavaliers, the worked example in the BPM 2.0 calculator sheet linked from
# https://www.basketball-reference.com/about/bpm2.html: each player's totals and listed position,
# then the sheet's position, offensive role, BPM and OBPM for him (`fixtures/bref/README.md`).
CLE = 1
CAVALIERS = FIXTURES / "2016-17_cavaliers_sheet.csv"
# The sheet's team inputs: 19881 minutes of players, a pace of 96.2, 9048 points, and a team
# rating of +3.0 (schedule-adjusted) with an offensive rating 4.8 above the league's.
CLE_MINUTES = 19881 / 5
CLE_POSSESSIONS = 96.2 * CLE_MINUTES / 48
CLE_PTS = 9048
# Points allowed that make the net rating the sheet's +3.0, and an average lead near its 1.42.
CLE_OPP_PTS = CLE_PTS - round(3.0 * CLE_POSSESSIONS / 100)
# The rest of the league, with no players, pins the league's offensive rating 4.8 below the
# Cavaliers' own.
REST = 2
REST_POSSESSIONS = 29 * CLE_POSSESSIONS
REST_PTS = round((CLE_PTS / CLE_POSSESSIONS * 100 - 4.8) / 100 * 30 * CLE_POSSESSIONS) - CLE_PTS


def _cavaliers() -> tuple[list[PlayerLine], dict[int, TeamLine], list[dict[str, str]]]:
    with CAVALIERS.open() as handle:
        rows = list(csv.DictReader(handle))
    lines = [
        PlayerLine(
            i,
            CLE,
            float(row["mp"]),
            float(row["listed"]),
            *(int(row[c]) for c in ("pts", "fga", "fg3", "fta", "orb", "drb")),
            *(int(row[c]) for c in ("ast", "stl", "blk", "tov", "pf")),
        )
        for i, row in enumerate(rows, 1)
    ]
    teams = {
        CLE: TeamLine(CLE, 82, CLE_MINUTES, CLE_POSSESSIONS, CLE_PTS, CLE_OPP_PTS),
        REST: TeamLine(REST, 82 * 29, 29 * CLE_MINUTES, REST_POSSESSIONS, REST_PTS, REST_PTS),
    }
    return lines, teams, rows


def test_the_sheets_cavaliers_are_reproduced() -> None:
    lines, teams, rows = _cavaliers()

    results = compute(lines, teams)

    assert [r.player_id for r in results] == list(range(1, 22))
    for result, row in zip(results, rows, strict=True):
        # The sheet rounds to a tenth, and stops shifting the team's positions after three rounds.
        assert result.position == pytest.approx(float(row["position"]), abs=0.06), row["player"]
        assert result.role == pytest.approx(float(row["role"]), abs=0.06), row["player"]
        assert result.bpm == pytest.approx(float(row["bpm"]), abs=0.11), row["player"]
        assert result.obpm == pytest.approx(float(row["obpm"]), abs=0.11), row["player"]
    # The team's BPMs, weighted by share of the minutes, sum to its rating: its net rating per
    # 100 possessions (the sheet's +3.0, to the rounding of the points) and the lead's effect.
    margin = CLE_PTS - CLE_OPP_PTS
    team_total = sum(r.bpm * r.minutes / CLE_MINUTES for r in results)
    assert team_total == pytest.approx(margin / CLE_POSSESSIONS * 100 + 0.175 * margin / 82 / 2)


def bref_season(
    *, coarse: bool = False
) -> tuple[list[PlayerLine], dict[int, TeamLine], dict[str, dict[str, str]]]:
    """The 2025-26 fixture as lines, with each team's possessions estimated from its totals, and
    the published rows by player-team; `coarse` lists each player as NBA.com would."""
    with (FIXTURES / "2025-26_teams.csv").open() as handle:
        team_rows = list(csv.DictReader(handle))
    with (FIXTURES / "2025-26_players.csv").open() as handle:
        player_rows = list(csv.DictReader(handle))
    team_ids = {row["team"]: i for i, row in enumerate(team_rows, 1)}
    teams = {
        team_ids[row["team"]]: TeamLine(
            team_ids[row["team"]],
            int(row["g"]),
            int(row["mp"]) / 5,
            possessions(
                TeamBox(*(int(row[c]) for c in BOX)),
                TeamBox(*(int(row[f"opp_{c}"]) for c in BOX)),
            ),
            int(row["pts"]),
            int(row["opp_pts"]),
        )
        for row in team_rows
    }
    lines = [
        PlayerLine(
            i,
            team_ids[row["team"]],
            float(row["mp"]),
            listed_position(NBA_POSITIONS[row["pos"]]) if coarse else BREF_POSITIONS[row["pos"]],
            int(row["pts"]),
            int(row["fga"]),
            int(row["fg3"]),
            int(row["fta"]),
            int(row["orb"]),
            int(row["drb"]),
            int(row["ast"]),
            int(row["stl"]),
            int(row["blk"]),
            int(row["tov"]),
            int(row["pf"]),
        )
        for i, row in enumerate(player_rows, 1)
    ]
    published = {f"{row['bref_id']}/{row['team']}": row for row in player_rows}
    return lines, teams, published


def test_possessions_estimate_matches_basketball_references_pace() -> None:
    _, teams, _ = bref_season()
    with (FIXTURES / "2025-26_teams.csv").open() as handle:
        paces = {i: float(row["pace"]) for i, row in enumerate(csv.DictReader(handle), 1)}

    for team_id, team in teams.items():
        # Pace is per 48 minutes; the fixture's `tov` is the team's, team turnovers included.
        assert team.possessions == pytest.approx(paces[team_id] * team.minutes / 48, rel=0.006)


# Five 2025-26 players and their published BPM, read on 2026-10-23 from
# https://www.basketball-reference.com/leagues/NBA_2026_advanced.html (the fixture's `bpm`).
PUBLISHED = {
    "jokicni01/DEN": ("Nikola Jokić", 14.2),
    "gilgesh01/OKC": ("Shai Gilgeous-Alexander", 11.7),
    "duranke01/HOU": ("Kevin Durant", 4.5),
    "edwaran01/MIN": ("Anthony Edwards", 4.5),
    "brookdi01/PHO": ("Dillon Brooks", -2.3),
}


def test_five_published_2025_26_values_are_within_a_point() -> None:
    lines, teams, published = bref_season()
    by_line = {line.player_id: line for line in lines}
    keys = {key: i for i, key in enumerate(published, 1)}

    results = {r.player_id: r for r in compute(lines, teams)}

    for key, (name, value) in PUBLISHED.items():
        assert published[key]["player"] == name
        assert float(published[key]["bpm"]) == value
        result = results[keys[key]]
        assert by_line[result.player_id].team_id == teams[result.team_id].team_id
        assert result.bpm == pytest.approx(value, abs=1.0), name


@pytest.mark.parametrize("coarse", [False, True], ids=["bref_positions", "nba_positions"])
def test_every_regular_is_within_a_point_and_the_league_averages_to_zero(coarse: bool) -> None:
    lines, teams, published = bref_season(coarse=coarse)
    keys = list(published)

    results = compute(lines, teams)

    assert len(results) == len(lines) == 661
    regulars = [(r, published[keys[r.player_id - 1]]) for r in results if r.minutes >= 1000]
    assert len(regulars) == 263
    for result, row in regulars:
        assert result.bpm == pytest.approx(float(row["bpm"]), abs=1.0), row["player"]
        assert result.obpm == pytest.approx(float(row["obpm"]), abs=1.0), row["player"]
    minutes = sum(r.minutes for r in results)
    assert sum(r.bpm * r.minutes for r in results) / minutes == pytest.approx(0.0, abs=0.2)
    assert sum(r.obpm * r.minutes for r in results) / minutes == pytest.approx(0.0, abs=0.2)


def test_shift_to_mean_adds_one_constant_and_clamps() -> None:
    values = [1.4, 2.0, 2.5, 6.0]
    weights = [1000.0, 2000.0, 500.0, 1500.0]

    shifted = shift_to_mean(values, weights)

    assert sum(v * w for v, w in zip(shifted, weights, strict=True)) / sum(
        weights
    ) == pytest.approx(3.0, abs=1e-9)
    # The last is clamped at 5.0; the rest moved together, by about 0.24.
    assert shifted[3] == 5.0
    assert shifted[1] - shifted[0] == pytest.approx(0.6)
    assert shifted[0] == pytest.approx(1.64, abs=0.01)
    assert shift_to_mean([0.0, 7.0], [0.0, 0.0]) == [1.0, 5.0]
    assert shift_to_mean([4.2], [100.0]) == pytest.approx([3.0])


def test_snapshot_rows_merge_a_traded_players_teams_by_minutes_and_skip_under_a_minute() -> None:
    day = date(2026, 10, 23)
    results = [
        PlayerBpm(7, 2, 300.0, 3.0, 3.0, 2.0, 1.0),
        PlayerBpm(7, 1, 100.0, 3.0, 3.0, -2.0, -1.0),
        PlayerBpm(8, 1, 0.9, 3.0, 3.0, 30.0, 20.0),
        PlayerBpm(9, 1, 0.5, 3.0, 3.0, 30.0, 20.0),
        PlayerBpm(9, 2, 0.5, 3.0, 3.0, 10.0, 10.0),
    ]

    assert snapshot_rows(day, results) == [
        ImpactSnapshotRow(day, 7, "bpm_local", 1.0, 0.5, 0.5, 400.0),
        ImpactSnapshotRow(day, 9, "bpm_local", 20.0, 15.0, 5.0, 1.0),
    ]


@pytest.mark.parametrize(
    ("position", "scale"),
    [("G", 1.5), ("G-F", 2.5), ("F-G", 2.5), ("F", 3.5), ("F-C", 4.5), ("C-F", 4.5), ("C", 5.0)],
)
def test_listed_positions_are_on_the_five_point_scale(position: str, scale: float) -> None:
    assert listed_position(position) == scale


def test_an_unknown_or_missing_position_is_a_forward() -> None:
    assert listed_position(None) == listed_position("") == listed_position("PG") == 3.0


GSW = 1610612744
DEN = 1610612743
DAY = date(2026, 10, 20)
REGULAR = "0022600801"
NEXT = "0022600802"
PLAYOFF = "0042600801"
LATER = "0022600803"
ONE_SIDED = "0022600804"
GAMES = {
    REGULAR: (DAY, "regular", "final"),
    NEXT: (DAY + timedelta(days=1), "regular", "final"),
    PLAYOFF: (DAY + timedelta(days=1), "playoff", "final"),
    LATER: (DAY + timedelta(days=2), "regular", "scheduled"),
    ONE_SIDED: (DAY, "regular", "final"),
}
WARRIORS = [910001, 910002, 910003, 910004, 910005]
NUGGETS = [920001, 920002, 920003, 920004, 920005]
# A Warrior with half a minute in the first game.
BRIEF = 910006


def _player_game(
    game_id: str, player_id: int, team_id: int, seconds: int, pts: int, *, played: bool = True
) -> PlayerGame:
    """A player's line: `pts` on 5 of 12 from the field, 2 of 4 free throws, 1 and 4 boards, 3
    assists, a steal, a block, 2 turnovers and 2 fouls; all zero when he did not `play`."""
    stat = int(played)
    return PlayerGame(
        game_id=game_id,
        player_id=player_id,
        team_id=team_id,
        is_starter=False,
        seconds=seconds,
        dnp_reason=None,
        pts=pts,
        fgm=5 * stat,
        fga=12 * stat,
        fg3m=stat,
        fg3a=4 * stat,
        ftm=2 * stat,
        fta=4 * stat,
        oreb=stat,
        dreb=4 * stat,
        ast=3 * stat,
        stl=stat,
        blk=stat,
        tov=2 * stat,
        pf=2 * stat,
        plus_minus=0,
        source="stats",
    )


def _clear(db: sessionmaker[Session]) -> None:
    players = [*WARRIORS, *NUGGETS, BRIEF]
    with db.begin() as session:
        session.execute(
            delete(ImpactSnapshot).where(
                ImpactSnapshot.source == "bpm_local", ImpactSnapshot.player_id.in_(players)
            )
        )
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(list(GAMES))))
        session.execute(delete(Game).where(Game.game_id.in_(list(GAMES))))
        session.execute(delete(Player).where(Player.nba_player_id.in_(players)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """GSW and DEN's games: a regular-season game on DAY and the day after, a playoff game that
    day too, one scheduled two days on, and one on DAY with rows for GSW only. Five players a
    side, 48 minutes each, scoring 10 through 14 (GSW) and 15 through 19 (DEN), but in the first
    game BRIEF takes 30 seconds from the fifth Warrior; the first Warrior is listed at center. The
    second game's schedule score, 70-90, is not its players' 60-85."""
    _clear(db)
    with db.begin() as session:
        for game_id, (day, phase, status) in GAMES.items():
            session.add(
                Game(
                    game_id=game_id,
                    season="2026-27",
                    game_date=day,
                    phase=phase,
                    home_team_id=GSW,
                    away_team_id=DEN,
                    status=status,
                    home_score=70 if game_id == NEXT else None,
                    away_score=90 if game_id == NEXT else None,
                    home_b2b=False,
                    away_b2b=False,
                )
            )
        session.add(Player(nba_player_id=WARRIORS[0], name="Center", position="C"))
        session.flush()
        for game_id in (REGULAR, NEXT, PLAYOFF, ONE_SIDED):
            for i, player_id in enumerate(WARRIORS):
                seconds = 2850 if game_id == REGULAR and i == 4 else 2880
                session.add(_player_game(game_id, player_id, GSW, seconds, 10 + i))
            if game_id != ONE_SIDED:
                for i, player_id in enumerate(NUGGETS):
                    session.add(_player_game(game_id, player_id, DEN, 2880, 15 + i))
        session.add(_player_game(REGULAR, BRIEF, GSW, 30, 0, played=False))
    yield db
    _clear(db)


def test_season_lines_sum_the_regular_season_games_final_through_the_date(
    seeded: sessionmaker[Session],
) -> None:
    with seeded() as session:
        lines, teams = season_lines(session, "2026-27", DAY)
        later, later_teams = season_lines(session, "2026-27", DAY + timedelta(days=1))

    # Each side's box: 25 of 60 from the field, 20 free throws, 5 and 20 boards, 10 turnovers.
    estimate = 60 + 0.4 * 20 - 1.07 * (5 / 25) * 35 + 10
    assert teams == {
        DEN: TeamLine(DEN, 1, 48.0, estimate, 85, 60),
        GSW: TeamLine(GSW, 1, 48.0, estimate, 60, 85),
    }
    assert [(line.player_id, line.team_id) for line in lines] == [
        *((player_id, DEN) for player_id in NUGGETS),
        *((player_id, GSW) for player_id in WARRIORS),
        (BRIEF, GSW),
    ]
    center = next(line for line in lines if line.player_id == WARRIORS[0])
    assert center == PlayerLine(WARRIORS[0], GSW, 48.0, 5.0, 10, 12, 1, 4, 1, 4, 3, 1, 1, 2, 2)
    assert {line.listed_position for line in lines if line.player_id != WARRIORS[0]} == {3.0}
    assert [line.minutes for line in lines if line.team_id == GSW] == [48.0] * 4 + [47.5, 0.5]
    # The next day adds the second regular-season game, not the playoff game, scored as the
    # schedule has it.
    assert later_teams[GSW] == TeamLine(GSW, 2, 96.0, 2 * estimate, 130, 175)
    assert later_teams[DEN] == TeamLine(DEN, 2, 96.0, 2 * estimate, 175, 130)
    assert next(line for line in later if line.player_id == NUGGETS[0]).pts == 30


def _stored(db: sessionmaker[Session], day: date) -> list[ImpactSnapshotRow]:
    with db() as session:
        stored = session.scalars(
            select(ImpactSnapshot)
            .where(
                ImpactSnapshot.date == day,
                ImpactSnapshot.source == "bpm_local",
                ImpactSnapshot.player_id >= WARRIORS[0],
            )
            .order_by(ImpactSnapshot.player_id)
        )
        return [
            ImpactSnapshotRow(
                r.date, r.player_id, "bpm_local", r.value, r.o_value, r.d_value, r.minutes_basis
            )
            for r in stored
        ]


def test_write_bpm_writes_each_date_from_its_season_to_date_and_is_idempotent(
    seeded: sessionmaker[Session],
) -> None:
    first = write_bpm(seeded, DAY)
    rows = _stored(seeded, DAY)
    again = write_bpm(seeded, DAY)
    next_day = write_bpm(seeded, DAY + timedelta(days=1))

    assert first == {"season": "2026-27", "games": 1, "players": 10, "changed": 10, "removed": 0}
    assert again == {**first, "changed": 0}
    assert _stored(seeded, DAY) == rows
    assert [row.player_id for row in rows] == [*WARRIORS, *NUGGETS]
    assert [row.minutes_basis for row in rows] == [48.0] * 4 + [47.5] + [48.0] * 5
    for row in rows:
        assert row.o_value is not None
        assert row.d_value == pytest.approx(row.value - row.o_value, abs=0.002)
    # DEN outscored GSW, so its players rate higher; within a side, the scorers do.
    assert min(row.value for row in rows[5:]) > max(row.value for row in rows[:5])
    assert rows[9].value > rows[5].value
    assert next_day == {**first, "games": 2, "players": 10, "changed": 10}
    assert [row.minutes_basis for row in _stored(seeded, DAY + timedelta(days=1))] == [
        96.0,
        96.0,
        96.0,
        96.0,
        95.5,
        96.0,
        96.0,
        96.0,
        96.0,
        96.0,
    ]


def test_the_source_is_the_local_one() -> None:
    assert bpm.SOURCE == "bpm_local"

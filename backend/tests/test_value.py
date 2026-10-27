import csv
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, ImpactSnapshot, PlayerGame, RosterMembership
from closinglineup.derive import value
from closinglineup.derive.value import (
    PlayerSeason,
    available_seconds,
    compute_value,
    player_seasons,
    vorp,
    vorp_rows,
)
from closinglineup.impact import ImpactSnapshotRow
from tests.test_bpm import FIXTURES

# Every 2025-26 player's published VORP, read on 2026-10-27 from
# https://www.basketball-reference.com/leagues/NBA_2026_advanced.html, one row per row of
# `2025-26_players.csv` (`fixtures/bref/README.md`).
PUBLISHED_VORP = FIXTURES / "2025-26_vorp.csv"


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
BOX_COLUMNS = (
    *("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta"),
    *("oreb", "dreb", "ast", "stl", "blk", "tov", "pf", "plus_minus"),
)


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(ImpactSnapshot).where(ImpactSnapshot.player_id.in_(PLAYERS)))
        session.execute(delete(RosterMembership).where(RosterMembership.player_id.in_(PLAYERS)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(list(GAMES))))
        session.execute(delete(Game).where(Game.game_id.in_(list(GAMES))))


def _player_game(game_id: str, player_id: int, team_id: int, seconds: int) -> PlayerGame:
    """A line of `seconds` and nothing else: VORP reads only the minutes."""
    return PlayerGame(
        game_id=game_id,
        player_id=player_id,
        team_id=team_id,
        is_starter=False,
        seconds=seconds,
        dnp_reason=None,
        **dict.fromkeys(BOX_COLUMNS, 0),
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
    assert set(stored) == {("vorp_local", TRADED), ("vorp_local", BENCH), ("vorp_local", UNLISTED)}
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


def test_the_sources_are_the_local_ones() -> None:
    assert (value.VORP_SOURCE, value.WS_SOURCE) == ("vorp_local", "ws_local")

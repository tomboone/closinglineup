from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, PlayerGame, RosterMembership, RunStatus
from closinglineup.load.memberships import load_participation_spans

# A season no other test loads, and player IDs no real player has, so leftovers of other tests
# on the same database stay out of these.
SEASON = "2019-20"
PRIOR = "2018-19"
GSW, DEN, LAL = 1610612744, 1610612743, 1610612747
TRADED, STAYED = 9_000_001, 9_000_002
PLAYERS = (TRADED, STAYED)
# (game ID, date): TRADED plays the first two for GSW, the third for DEN, the fourth back with GSW.
GAMES = [
    ("0021900901", date(2019, 10, 23)),
    ("0021900902", date(2019, 10, 25)),
    ("0021900903", date(2019, 11, 2)),
    ("0021900904", date(2019, 11, 9)),
]
TEAMS_OF_TRADED = [GSW, GSW, DEN, GSW]

# deploy/README.md's coverage check: player_game rows of the season with no covering membership.
UNCOVERED = text(
    """
    SELECT count(*) FROM player_game pg JOIN game g USING (game_id)
    WHERE g.season = :season AND NOT EXISTS (
        SELECT 1 FROM roster_membership rm
        WHERE rm.player_id = pg.player_id AND rm.team_id = pg.team_id
          AND rm.start_date <= g.game_date
          AND (rm.end_date IS NULL OR rm.end_date >= g.game_date)
    )
    """
)


def _player_game(game_id: str, player_id: int, team_id: int, seconds: int) -> PlayerGame:
    counts = dict.fromkeys(
        ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk"),
        0,
    )
    return PlayerGame(
        game_id=game_id,
        player_id=player_id,
        team_id=team_id,
        is_starter=False,
        seconds=seconds,
        # A DNP is still a game with the team.
        dnp_reason=None if seconds else "cd",
        tov=0,
        pf=0,
        plus_minus=0,
        source="stats",
        **counts,
    )


def _clear(db: sessionmaker[Session]) -> None:
    game_ids = [game_id for game_id, _ in GAMES]
    with db.begin() as session:
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))
        session.execute(delete(RosterMembership).where(RosterMembership.player_id.in_(PLAYERS)))


@pytest.fixture
def season(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """`GAMES` loaded: TRADED in each, STAYED for LAL in the first and, as a DNP, the last."""
    _clear(db)
    with db.begin() as session:
        for game_id, day in GAMES:
            session.add(
                Game(
                    game_id=game_id,
                    season=SEASON,
                    game_date=day,
                    phase="regular",
                    home_team_id=GSW,
                    away_team_id=DEN,
                    status="final",
                    home_b2b=False,
                    away_b2b=False,
                )
            )
        session.flush()
        for (game_id, _), team_id in zip(GAMES, TEAMS_OF_TRADED, strict=True):
            session.add(_player_game(game_id, TRADED, team_id, 600))
        session.add(_player_game(GAMES[0][0], STAYED, LAL, 1200))
        session.add(_player_game(GAMES[-1][0], STAYED, LAL, 0))
    yield db
    _clear(db)


def _spans(db: sessionmaker[Session]) -> list[tuple[int, int, date, date | None, str]]:
    with db() as session:
        found = session.execute(
            select(
                RosterMembership.player_id,
                RosterMembership.team_id,
                RosterMembership.start_date,
                RosterMembership.end_date,
                RosterMembership.source,
            )
            .where(RosterMembership.player_id.in_(PLAYERS))
            .order_by(RosterMembership.player_id, RosterMembership.start_date)
        )
        return [(p, t, s, e, src) for p, t, s, e, src in found]


def _uncovered(db: sessionmaker[Session]) -> int:
    with db() as session:
        return session.execute(UNCOVERED, {"season": SEASON}).scalar_one()


def test_spans_cover_every_player_game_row_and_a_rerun_changes_nothing(
    season: sessionmaker[Session],
) -> None:
    assert _uncovered(season) == 6

    first = load_participation_spans(season, SEASON)
    second = load_participation_spans(season, SEASON)

    assert (first.status, first.key, first.rows, first.changed, first.removed) == (
        RunStatus.SUCCEEDED,
        SEASON,
        4,
        4,
        0,
    )
    assert (second.rows, second.changed, second.removed) == (4, 0, 0)
    assert _uncovered(season) == 0
    assert _spans(season) == [
        (TRADED, GSW, GAMES[0][1], GAMES[1][1], "player_game"),
        (TRADED, DEN, GAMES[2][1], GAMES[2][1], "player_game"),
        (TRADED, GSW, GAMES[3][1], GAMES[3][1], "player_game"),
        (STAYED, LAL, GAMES[0][1], GAMES[3][1], "player_game"),
    ]


def test_a_span_whose_start_moves_replaces_the_old_one_and_leaves_other_seasons_alone(
    season: sessionmaker[Session],
) -> None:
    load_participation_spans(season, SEASON)
    prior = RosterMembership(
        player_id=TRADED,
        team_id=GSW,
        start_date=date(2019, 3, 1),
        end_date=date(2019, 4, 10),
        contract_type="standard",
        source="player_game",
    )
    with season.begin() as session:
        session.add(prior)
        first_game = PlayerGame.game_id == GAMES[0][0]
        session.execute(delete(PlayerGame).where(first_game, PlayerGame.player_id == TRADED))

    result = load_participation_spans(season, SEASON)

    assert (result.rows, result.changed, result.removed) == (4, 1, 1)
    spans = _spans(season)
    assert (TRADED, GSW, GAMES[1][1], GAMES[1][1], "player_game") in spans
    assert (TRADED, GSW, GAMES[0][1], GAMES[1][1], "player_game") not in spans
    assert (TRADED, GSW, date(2019, 3, 1), date(2019, 4, 10), "player_game") in spans


def test_a_roster_membership_on_the_same_key_is_left_as_it_is(
    season: sessionmaker[Session],
) -> None:
    with season.begin() as session:
        session.add(
            RosterMembership(
                player_id=STAYED,
                team_id=LAL,
                start_date=GAMES[0][1],
                contract_type="two_way",
                source="commonteamroster",
            )
        )

    result = load_participation_spans(season, SEASON)

    assert (result.rows, result.changed) == (4, 3)
    assert (STAYED, LAL, GAMES[0][1], None, "commonteamroster") in _spans(season)
    assert _uncovered(season) == 0


def test_a_season_with_no_games_has_no_spans(season: sessionmaker[Session]) -> None:
    result = load_participation_spans(season, PRIOR)

    assert (result.status, result.rows, result.changed) == (RunStatus.SUCCEEDED, 0, 0)

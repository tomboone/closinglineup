from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from closinglineup.cli import app
from closinglineup.db.models import Game, Stint
from closinglineup.derive.garbage_time import (
    GarbageTimeRule,
    StintScore,
    classify,
    write_garbage_time,
)
from closinglineup.parse import pbp
from closinglineup.parse.rows import StintRow
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn
from closinglineup.sources.cache import RawCache
from tests.test_cli import ANSI_ESCAPE
from tests.test_pbp import FEED_2026_27_GAME, GAMES, OVERTIME_GAME, QUIRK_GAME, RECONCILE_GAME
from tests.test_pbp import FIXTURES as PBP_FIXTURES

RULE = GarbageTimeRule(((25, 540), (20, 360), (15, 180), (10, 60)))


def _game(*stints: tuple[int, float, int]) -> list[StintScore]:
    """One team's stints in order, each `(period, seconds left in it, points net over it)`."""
    return [
        StintScore(period, 720 - left, max(net, 0), max(-net, 0)) for period, left, net in stints
    ]


# Leads by 30 after three quarters and stays out of reach.
BLOWOUT = _game(
    (1, 720, 10),
    (2, 720, 10),
    (3, 720, 10),
    (4, 720, 0),
    (4, 600, 0),
    (4, 540, -2),
    (4, 400, 0),
    (4, 200, -3),
    (4, 30, 0),
)
# Never more than six apart.
CLOSE = _game(
    (1, 720, 6),
    (2, 720, -4),
    (3, 720, -4),
    (4, 720, 3),
    (4, 360, -2),
    (4, 180, 2),
    (4, 60, -1),
    (4, 20, 0),
)
# Up 26 with nine minutes left and 20 with six, then the lead melts to eight, then is rebuilt to
# twelve.
COMEBACK = _game(
    (1, 720, 26),
    (4, 540, -6),
    (4, 360, -12),
    (4, 200, 4),
    (4, 50, 0),
)


def test_a_blowout_flags_every_fourth_quarter_stint_from_the_threshold_on() -> None:
    assert classify(BLOWOUT, RULE) == [False] * 3 + [False, False, True, True, True, True]


def test_a_close_game_flags_nothing() -> None:
    assert classify(CLOSE, RULE) == [False] * len(CLOSE)


def test_a_comeback_within_10_clears_every_earlier_flag() -> None:
    # 26 at 9:00 and 20 at 6:00 are flagged alone; the stint starting at +8 clears both, and the
    # rebuilt +12 with 50 seconds left is flagged again, with nothing after it.
    assert classify(COMEBACK[:3], RULE) == [False, True, True]
    assert classify(COMEBACK, RULE) == [False, False, False, False, True]


def test_the_thresholds_are_inclusive_and_a_later_10_point_margin_clears_nothing() -> None:
    at_the_line = _game((1, 720, 25), (4, 540, -15), (4, 40, 0))

    assert classify(at_the_line, RULE) == [False, True, True]
    assert classify(_game((1, 720, 25), (4, 541, 0)), RULE) == [False, False]
    assert classify(_game((1, 720, 24), (4, 540, 0)), RULE) == [False, False]


def test_a_flagged_last_stint_stands_however_the_game_ends() -> None:
    # Up 12 with 50 seconds left, and the game ends up 4: no later stint starts within 10.
    assert classify(_game((1, 720, 12), (4, 50, -8)), RULE) == [False, True]


def test_only_the_fourth_quarter_counts_and_overtime_clears_it() -> None:
    assert classify(_game((1, 720, 40), (3, 30, 0)), RULE) == [False, False]
    tied = _game((1, 720, 12), (4, 40, -12), (5, 720, 0), (5, 30, 0))
    assert classify(tied, RULE) == [False] * 4


def test_both_teams_views_get_the_same_flags() -> None:
    mirrored = [StintScore(s.period, s.start_sec, s.pts_against, s.pts_for) for s in COMEBACK]

    assert classify(mirrored, RULE) == classify(COMEBACK, RULE)


def test_the_rule_is_read_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://closinglineup@localhost/closinglineup")
    monkeypatch.setenv("GARBAGE_TIME_RULE", "30:600,12:90")

    rule = GarbageTimeRule.from_settings(Settings())

    assert rule == GarbageTimeRule(((30, 600), (12, 90)), comeback_margin=10)


@pytest.fixture(scope="module")
def fixture_stints(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[StintRow]]:
    """Every fixture game's stints, as `parse.pbp` emits them."""
    cache = RawCache(tmp_path_factory.mktemp("data"))
    stints: dict[str, list[StintRow]] = {}
    for game_id in GAMES:
        body = (PBP_FIXTURES / nba_cdn.PLAYBYPLAY / f"{game_id}.json").read_bytes()
        cache.put(nba_cdn.SOURCE, nba_cdn.PLAYBYPLAY, game_id, body, url="u", status=200)
        pbp.materialize(cache, game_id)
        stints[game_id] = pbp.rows_for_game(cache.root.parent, game_id).stints
    return stints


@pytest.mark.parametrize(
    ("game_id", "flagged"),
    [
        # Within seven through the fourth, then tied into overtime.
        (OVERTIME_GAME, 0),
        # Never more than seven apart in the fourth.
        (QUIRK_GAME, 0),
        # Up 12 with 47 seconds left.
        (FEED_2026_27_GAME, 1),
        # Up 22 at 5:39, and 18 to 20 from 2:36 on; 18 at 5:09 is short of 20 at 6:00.
        (RECONCILE_GAME, 4),
    ],
)
def test_the_fixture_games_flag_counts(
    fixture_stints: dict[str, list[StintRow]], game_id: str, flagged: int
) -> None:
    rows = fixture_stints[game_id]
    teams = sorted({row.team_id for row in rows})

    counts = [
        sum(classify([StintScore.of(row) for row in rows if row.team_id == team_id], RULE))
        for team_id in teams
    ]

    assert counts == [flagged, flagged]


# A season no other test loads.
SEASON = "2016-17"
DAY = date(2016, 11, 1)
BLOWOUT_GAME = "0021600101"
CLOSE_GAME = "0021600102"
# A game the day after, which deriving DAY leaves alone.
NEXT_GAME = "0021600201"
GAME_IDS = (BLOWOUT_GAME, CLOSE_GAME, NEXT_GAME)
HOME, AWAY = 1610612744, 1610612743
FIVE = "9200001-9200002-9200003-9200004-9200005"
OTHER_FIVE = "9200011-9200012-9200013-9200014-9200015"


def _stints(game_id: str, stints: list[StintScore]) -> list[Stint]:
    """Both teams' rows for `stints`, the home team's view."""
    rows: list[Stint] = []
    for stint in stints:
        for team_id, key, opp_key, pts_for, pts_against in (
            (HOME, FIVE, OTHER_FIVE, stint.pts_for, stint.pts_against),
            (AWAY, OTHER_FIVE, FIVE, stint.pts_against, stint.pts_for),
        ):
            rows.append(
                Stint(
                    game_id=game_id,
                    period=stint.period,
                    start_sec=stint.start_sec,
                    end_sec=stint.start_sec + 1,
                    team_id=team_id,
                    lineup_key=key,
                    opp_lineup_key=opp_key,
                    possessions=1,
                    pts_for=pts_for,
                    pts_against=pts_against,
                )
            )
    return rows


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(Stint).where(Stint.game_id.in_(GAME_IDS)))
        session.execute(delete(Game).where(Game.game_id.in_(GAME_IDS)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """A blowout and a close game on DAY, and a blowout the day after."""
    _clear(db)
    with db.begin() as session:
        for game_id, day in (
            (BLOWOUT_GAME, DAY),
            (CLOSE_GAME, DAY),
            (NEXT_GAME, date(2016, 11, 2)),
        ):
            session.add(
                Game(
                    game_id=game_id,
                    season=SEASON,
                    game_date=day,
                    phase="regular",
                    home_team_id=HOME,
                    away_team_id=AWAY,
                    status="final",
                    home_b2b=False,
                    away_b2b=False,
                )
            )
        session.flush()
        session.add_all(
            [
                *_stints(BLOWOUT_GAME, BLOWOUT),
                *_stints(CLOSE_GAME, CLOSE),
                *_stints(NEXT_GAME, BLOWOUT),
            ]
        )
    yield db
    _clear(db)


def _flags(db: sessionmaker[Session], game_id: str) -> dict[int, list[bool | None]]:
    with db() as session:
        stints = session.scalars(
            select(Stint)
            .where(Stint.game_id == game_id)
            .order_by(Stint.team_id, Stint.period, Stint.start_sec)
        ).all()
    flags: dict[int, list[bool | None]] = {}
    for stint in stints:
        flags.setdefault(stint.team_id, []).append(stint.is_garbage_time)
    return flags


def test_write_garbage_time_flags_both_teams_rows_of_the_dates_games_idempotently(
    seeded: sessionmaker[Session],
) -> None:
    first = write_garbage_time(seeded, DAY, RULE)
    again = write_garbage_time(seeded, DAY, RULE)

    stints = 2 * (len(BLOWOUT) + len(CLOSE))
    assert first == {"games": 2, "stints": stints, "flagged": 8, "changed": stints}
    assert again == {**first, "changed": 0}
    blowout = [False] * 5 + [True] * 4
    assert _flags(seeded, BLOWOUT_GAME) == {HOME: blowout, AWAY: blowout}
    assert _flags(seeded, CLOSE_GAME) == {HOME: [False] * 8, AWAY: [False] * 8}
    assert _flags(seeded, NEXT_GAME) == {HOME: [None] * 9, AWAY: [None] * 9}


def test_write_garbage_time_rewrites_a_flag_the_stints_no_longer_support(
    seeded: sessionmaker[Session],
) -> None:
    write_garbage_time(seeded, DAY, RULE)

    stricter = write_garbage_time(seeded, DAY, GarbageTimeRule(((25, 60),)))

    assert stricter["flagged"] == 2
    assert stricter["changed"] == 6
    assert _flags(seeded, BLOWOUT_GAME)[HOME] == [False] * 8 + [True]


def test_derive_flags_each_date_in_the_range_and_re_runs_unchanged(
    seeded: sessionmaker[Session],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runner = CliRunner()
    args = ["derive", "--from", "2016-11-01", "--to", "2016-11-02"]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert first.output.splitlines() == [
        "derive 2016-11-01: garbage time: 2 games, 34 stints, 8 flagged, 34 changed",
        "derive 2016-11-02: garbage time: 1 games, 18 stints, 8 flagged, 18 changed",
    ]
    assert second.output.splitlines() == [
        "derive 2016-11-01: garbage time: 2 games, 34 stints, 8 flagged, 0 changed",
        "derive 2016-11-02: garbage time: 1 games, 18 stints, 8 flagged, 0 changed",
    ]


def test_derive_needs_dates() -> None:
    result = CliRunner().invoke(app, ["derive"])

    assert result.exit_code == 2
    assert "pass --date or --from/--to" in ANSI_ESCAPE.sub("", result.output)

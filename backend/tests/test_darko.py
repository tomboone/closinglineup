import json
import math
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup import darko
from closinglineup.clock import FakeClock
from closinglineup.db.models import ImpactSnapshot, Player
from closinglineup.heartbeat import Heartbeat
from closinglineup.impact import ImpactSnapshotRow, replace_snapshot
from closinglineup.names import NameIndex
from closinglineup.overlays import by_name
from closinglineup.scheduler import Jobs
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.darko import (
    COLUMNS,
    URL,
    DarkoBoard,
    DarkoError,
    DarkoRow,
    fetch,
    hydrate,
    parse,
)
from closinglineup.teams import by_tricode
from tests.fakes import RoutedGet
from tests.test_scheduler import make_jobs

FIXTURES = Path(__file__).parent / "fixtures" / "darko"
BOARD = FIXTURES / "leaderboard" / "2026-10-23.json"
DAY = date(2026, 10, 23)
# 09:00 Eastern on the day, as the `daily_overlays` job runs.
FETCHED = datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
JOKIC = DarkoRow(203999, "Nikola Jokic", 1610612743, 7.0854, 5.13644, 1.94895, 32.6348)
# The jobs get their database from the test's fixture, never from the settings.
UNUSED_DB = "postgresql+psycopg://unused@127.0.0.1:1/x"
ENABLED = Settings(database_url=UNUSED_DB, darko_enabled=True)
DISABLED = Settings(database_url=UNUSED_DB, darko_enabled=False)


def _flatten(value: Any, flat: list[Any]) -> int:
    """`value` appended to devalue's flat list; its index."""
    index = len(flat)
    flat.append(None)
    if isinstance(value, dict):
        flat[index] = {key: _flatten(item, flat) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    elif isinstance(value, list):
        flat[index] = [_flatten(item, flat) for item in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    else:
        flat[index] = value
    return index


def _payload(root: dict[str, Any]) -> dict[str, Any]:
    flat: list[Any] = []
    _flatten(root, flat)
    return {"type": "data", "nodes": [None, {"type": "data", "data": flat, "uses": {}}]}


def _line(**overrides: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "nba_id": JOKIC.nba_id,
        "player_name": JOKIC.player,
        "tm_id": JOKIC.team_id,
        "season": 2027,
        "dpm": JOKIC.dpm,
        "o_dpm": JOKIC.o_dpm,
        "d_dpm": JOKIC.d_dpm,
        "x_minutes": JOKIC.x_minutes,
    }
    return {**line, **overrides}


def _board(*lines: dict[str, Any], through: str | None = "2026-10-22") -> dict[str, Any]:
    keys = list(COLUMNS)
    return {
        "players": {"keys": keys, "values": [[line.get(key) for line in lines] for key in keys]},
        "seasons": [2027],
        "selectedSeason": None,
        "asOf": None,
        "ratingsThrough": through,
    }


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "board.json"
    path.write_text(json.dumps(data))
    return path


def _jobs(
    settings: Settings, tmp_path: Path, db: sessionmaker[Session], http: httpx.Client
) -> Jobs:
    return make_jobs(settings, FakeClock(FETCHED), tmp_path, db, RoutedGet({}), Heartbeat({}, http))


def test_the_fixture_parses_every_line_with_an_nba_id_a_split_dpm_and_minutes() -> None:
    board = parse(BOARD)

    # Counted from the fixture's columns, not the parser: 519 lines, each with an `nba_id`.
    assert len(board.rows) == 519
    assert all(row.nba_id for row in board.rows)
    assert len({row.nba_id for row in board.rows}) == 519
    assert all(row.team_id and row.x_minutes for row in board.rows)
    assert len({row.team_id for row in board.rows}) == 30
    assert board.rows[0] == JOKIC
    assert board.ratings_through == date(2026, 10, 22)
    assert board.season == 2027
    assert board.no_value == []


def test_a_line_with_no_nba_id_keeps_its_name_and_team(tmp_path: Path) -> None:
    data = _payload(_board(_line(nba_id=None), _line(nba_id=0, player_name="Someone Else")))

    board = parse(_write(tmp_path, data))

    assert [(row.nba_id, row.player, row.team_id) for row in board.rows] == [
        (None, "Nikola Jokic", 1610612743),
        (None, "Someone Else", 1610612743),
    ]


def test_a_line_with_no_dpm_has_no_row_and_is_listed(tmp_path: Path) -> None:
    data = _payload(_board(_line(dpm=None), _line(player_name="Other", o_dpm="n/a"), _line()))

    board = parse(_write(tmp_path, data))

    assert [row.player for row in board.rows] == ["Nikola Jokic"]
    assert board.no_value == ["Nikola Jokic", "Other"]


def test_an_unknown_team_and_missing_minutes_are_none(tmp_path: Path) -> None:
    data = _payload(_board(_line(tm_id=0, x_minutes=None), _line(tm_id=None, season="x")))

    board = parse(_write(tmp_path, data))

    assert [(row.team_id, row.x_minutes) for row in board.rows] == [(None, None), (None, 32.6348)]
    assert board.season == 2027


def test_the_season_is_none_where_the_lines_disagree_and_no_through_is_none(
    tmp_path: Path,
) -> None:
    board = parse(_write(tmp_path, _payload(_board(_line(), _line(season=2026), through=None))))

    assert (board.season, board.ratings_through) == (None, None)


def test_hydrate_reads_devalue_specials_and_refuses_tagged_types() -> None:
    assert hydrate([{"a": 1, "b": -1, "c": -3, "d": 2}, [3, -2], "x", True]) == {
        "a": [True, None],
        "b": None,
        "c": pytest.approx(math.nan, nan_ok=True),
        "d": "x",
    }
    with pytest.raises(DarkoError, match="unsupported devalue type 'Date'"):
        hydrate([["Date", "2026-10-22"]])
    with pytest.raises(DarkoError, match="unknown devalue special -7"):
        hydrate([[-7]])


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"type": "data", "nodes": [None]}, "has no leaderboard node"),
        (_payload({"players": "none"}), "has no leaderboard node"),
        (
            _payload({"players": {"keys": ["nba_id", "dpm"], "values": [[1], [2.0]]}}),
            "players lack player_name, tm_id, season, o_dpm, d_dpm, x_minutes",
        ),
        (_payload(_board(_line(player_name=None))), "a line has no player name"),
    ],
)
def test_a_file_that_is_not_the_board_raises(
    tmp_path: Path, data: dict[str, Any], message: str
) -> None:
    with pytest.raises(DarkoError, match=message):
        parse(_write(tmp_path, data))


@respx.mock
def test_fetch_caches_the_board_under_the_day_and_a_rerun_reads_it_back(tmp_path: Path) -> None:
    route = respx.get(URL).respond(200, content=BOARD.read_bytes())
    cache = RawCache(tmp_path, FakeClock(FETCHED))

    with httpx.Client() as client:
        path = fetch(client, cache, DAY)
        again = fetch(client, cache, DAY)
        forced = fetch(client, cache, DAY, force=True)

    assert path == again == forced == tmp_path / "raw" / "darko" / "leaderboard" / "2026-10-23.json"
    assert path.read_bytes() == BOARD.read_bytes()
    assert route.call_count == 2
    assert cache.meta("darko", "leaderboard", "2026-10-23") == {
        "fetched_at": FETCHED.isoformat(),
        "url": URL,
        "status": 200,
        "params": {},
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, content=b"<html>"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"type": "data"}),
    ],
)
@respx.mock
def test_a_bad_response_raises_and_is_not_cached(tmp_path: Path, response: httpx.Response) -> None:
    respx.get(URL).mock(return_value=response)

    with httpx.Client() as client, pytest.raises((httpx.HTTPStatusError, DarkoError)):
        fetch(client, RawCache(tmp_path, FakeClock(FETCHED)), DAY)

    assert not (tmp_path / "raw").exists()


def test_rows_resolve_by_nba_id_else_by_name_on_the_teams_roster(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bos = by_tricode("BOS").nba_team_id
    index = NameIndex([(203999, "Nikola Jokić"), (1, "Jaylen Brown"), (2, "Jaylen Brown")], {})
    lines = [
        JOKIC,
        DarkoRow(None, "Jaylen Brown", bos, 2.0, 1.5, 0.5, 30.0),
        DarkoRow(None, "Jaylen Brown", by_tricode("LAL").nba_team_id, 1.0, 0.5, 0.5, None),
        DarkoRow(None, "Nikola Jokic", None, 7.0, 5.0, 2.0, 33.0),
        DarkoRow(None, "Nobody Known", bos, 0.0, 0.0, 0.0, 0.0),
        DarkoRow(999, "Just Signed", bos, 0.5, 0.5, 0.0, 10.0),
    ]
    board = DarkoBoard(DAY, 2027, lines, [])

    resolved = darko.resolve_rows(board, DAY, index, {(bos, DAY): {2}}, {203999, 1, 2})

    assert resolved.rows == [
        ImpactSnapshotRow(DAY, 203999, "darko", 7.0854, 5.13644, 1.94895, 32.6348),
        ImpactSnapshotRow(DAY, 2, "darko", 2.0, 1.5, 0.5, 30.0),
        ImpactSnapshotRow(DAY, 999, "darko", 0.5, 0.5, 0.0, 10.0),
    ]
    assert (resolved.by_id, resolved.by_name) == (2, 1)
    # Two Jaylen Browns and neither on the Lakers; Jokić by name is the row his ID already has;
    # 999 is stored under the board's ID though the player table lacks it.
    assert resolved.unresolved == ["Jaylen Brown", "Nobody Known"]
    assert resolved.duplicates == ["Nikola Jokic"]
    assert resolved.unknown_players == ["Just Signed"]
    assert "DARKO player not resolved" in caplog.text
    assert "DARKO names a player twice" in caplog.text
    assert "DARKO player not in the player table" in caplog.text


def test_the_overlay_is_registered_in_the_daily_family() -> None:
    overlay = by_name("darko")

    assert (overlay.family, overlay.run) == ("daily", darko.run)


@respx.mock(assert_all_called=False)
def test_the_overlay_is_a_no_op_with_no_request_when_darko_is_disabled(tmp_path: Path) -> None:
    route = respx.get(URL).respond(200, content=BOARD.read_bytes())
    with httpx.Client() as http:
        detail = darko.run(_jobs(DISABLED, tmp_path, sessionmaker(), http), DAY)

    assert detail == {"enabled": False, "rows": 0}
    assert not route.called
    assert not (tmp_path / "raw").exists()


@respx.mock(assert_all_called=False)
def test_a_first_run_for_a_date_other_than_today_raises_before_any_request(
    tmp_path: Path,
) -> None:
    route = respx.get(URL).respond(200, content=BOARD.read_bytes())
    with (
        httpx.Client() as http,
        pytest.raises(
            ValueError, match="the board is today's, 2026-10-23; 2026-10-01 is not cached"
        ),
    ):
        darko.run(_jobs(ENABLED, tmp_path, sessionmaker(), http), date(2026, 10, 1))

    assert not route.called


def _stored(db: sessionmaker[Session], day: date = DAY) -> list[ImpactSnapshotRow]:
    with db() as session:
        rows = session.scalars(
            select(ImpactSnapshot)
            .where(ImpactSnapshot.date == day, ImpactSnapshot.source == "darko")
            .order_by(ImpactSnapshot.player_id)
        )
        return [
            ImpactSnapshotRow(
                r.date, r.player_id, "darko", r.value, r.o_value, r.d_value, r.minutes_basis
            )
            for r in rows
        ]


@pytest.fixture
def cleared(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """The database with no `darko` rows for the day, before and after, and the fixture board's
    players in `player` for the test's duration, under the board's own IDs and names. Only the
    players the table lacked are added, and they are removed again: the board spells names its
    own way, which would change what other tests' names resolve to."""

    def clear() -> None:
        with db.begin() as session:
            session.execute(
                delete(ImpactSnapshot).where(
                    ImpactSnapshot.date == DAY, ImpactSnapshot.source == "darko"
                )
            )

    clear()
    with db.begin() as session:
        players = [{"nba_player_id": row.nba_id, "name": row.player} for row in parse(BOARD).rows]
        stmt = insert(Player).values(players).on_conflict_do_nothing()
        added = list(session.scalars(stmt.returning(Player.nba_player_id)))
    yield db
    clear()
    with db.begin() as session:
        session.execute(delete(Player).where(Player.nba_player_id.in_(added)))


@respx.mock
def test_the_overlay_stores_the_board_and_a_rerun_leaves_it_as_it_was(
    cleared: sessionmaker[Session], tmp_path: Path
) -> None:
    route = respx.get(URL).respond(200, content=BOARD.read_bytes())
    with httpx.Client() as http:
        jobs = _jobs(ENABLED, tmp_path, cleared, http)
        detail = jobs.overlay("darko", DAY)
        first = _stored(cleared)
        again = jobs.overlay("darko", DAY)

    assert detail == {
        "enabled": True,
        "file": "2026-10-23.json",
        "ratings_through": "2026-10-22",
        "season": 2027,
        "rows": 519,
        "by_id": 519,
        "by_name": 0,
        "changed": 519,
        "removed": 0,
        "no_value": [],
        "unknown_players": [],
        "unresolved": [],
        "duplicates": [],
    }
    assert len(first) == 519
    assert ImpactSnapshotRow(DAY, 203999, "darko", 7.0854, 5.13644, 1.94895, 32.6348) in first
    assert again == {**detail, "changed": 0}
    assert _stored(cleared) == first
    # The second run read the file the first cached.
    assert route.call_count == 1


def test_replace_snapshot_writes_only_what_changed_and_drops_players_the_board_lost(
    cleared: sessionmaker[Session],
) -> None:
    kept = ImpactSnapshotRow(DAY, 1, "darko", 1.0, 0.5, 0.5, 20.0)
    changed = ImpactSnapshotRow(DAY, 2, "darko", 2.0, 1.0, 1.0, None)
    dropped = ImpactSnapshotRow(DAY, 3, "darko", 3.0, None, None, 30.0)
    with cleared.begin() as session:
        assert replace_snapshot(session, DAY, "darko", [kept, changed, dropped]) == (3, 0)
    with cleared() as session:
        before = {r.player_id: r.updated_at for r in session.scalars(select(ImpactSnapshot))}

    newer = ImpactSnapshotRow(DAY, 2, "darko", 2.5, 1.0, 1.5, None)
    with cleared.begin() as session:
        assert replace_snapshot(session, DAY, "darko", [kept, newer]) == (1, 1)
    with cleared.begin() as session:
        assert replace_snapshot(session, DAY, "darko", [kept, newer]) == (0, 0)

    assert _stored(cleared) == [kept, newer]
    with cleared() as session:
        after = {r.player_id: r.updated_at for r in session.scalars(select(ImpactSnapshot))}
    assert after[1] == before[1]
    assert after[2] > before[2]

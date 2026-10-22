from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import FakeClock
from closinglineup.db.models import (
    Base,
    Game,
    LoadLog,
    Matchup,
    PlayerGame,
    RunStatus,
)
from closinglineup.load.loader import (
    NATURAL_KEYS,
    LoadError,
    LoadResult,
    load_dates,
    load_table,
    raise_for_failures,
)
from closinglineup.load.parquet import ROW_MODELS, ParquetStore
from closinglineup.parse.rows import PlayerGameRow
from closinglineup.pipeline import ParseReport, parse_date
from tests.test_pipeline import DAY, GAME, PLAYER_GAMES, TZ, UNCACHED, cache_fixture_game

START = datetime(2026, 10, 22, 12, tzinfo=UTC)
DAY_BEFORE = DAY - timedelta(days=1)
DATES = (DAY_BEFORE, DAY)

type Snapshot = dict[str, list[dict[str, Any]]]


def clear_fixture_game(db: sessionmaker[Session]) -> None:
    """Delete the fixture games' rows and the `load_log` rows of their dates, which earlier runs
    on the same database may have left. Players and roster memberships stay: they load by
    upsert, so a leftover row only reads as unchanged."""
    games = (GAME, UNCACHED)
    with db.begin() as session:
        session.execute(delete(Matchup).where(Matchup.game_id.in_(games)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(games)))
        session.execute(delete(Game).where(Game.game_id.in_(games)))
        session.execute(delete(LoadLog).where(LoadLog.game_date.in_(DATES)))


@pytest.fixture
def clean_db(db: sessionmaker[Session]) -> sessionmaker[Session]:
    clear_fixture_game(db)
    return db


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def store(tmp_path: Path) -> ParquetStore:
    return ParquetStore(tmp_path)


@pytest.fixture
def parsed(tmp_path: Path, store: ParquetStore) -> ParseReport:
    cache = cache_fixture_game(tmp_path)
    parse_date(cache, store, DAY_BEFORE, TZ)
    return parse_date(cache, store, DAY, TZ)


def _snapshot(db: sessionmaker[Session], store: ParquetStore, day: date) -> Snapshot:
    """Every column, timestamps included, of the rows the date's files hold, by table."""
    snapshot: Snapshot = {}
    with db() as session:
        for name in ROW_MODELS:
            target = Base.metadata.tables[name]
            key = NATURAL_KEYS[name]
            data = store.read(name, day)
            keys = list(zip(*(data.column(k).to_pylist() for k in key), strict=True))
            found = session.execute(
                select(target)
                .where(tuple_(*(target.c[k] for k in key)).in_(keys))
                .order_by(*(target.c[k] for k in key))
            )
            snapshot[name] = [dict(row._mapping) for row in found]  # pyright: ignore[reportPrivateUsage]
    return snapshot


def _logs(db: sessionmaker[Session], day: date) -> list[LoadLog]:
    with db() as session:
        query = select(LoadLog).where(LoadLog.game_date == day).order_by(LoadLog.table_name)
        return list(session.scalars(query))


def test_parse_then_load_fills_every_table_and_a_reload_changes_no_row(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    first = load_dates(clean_db, store, clock, [DAY])

    assert [(r.table, r.status, r.rows) for r in first] == [
        (name, RunStatus.SUCCEEDED, parsed.rows[name]) for name in ROW_MODELS
    ]
    snapshot = _snapshot(clean_db, store, DAY)
    assert [row["game_id"] for row in snapshot["game"]] == [GAME, UNCACHED]
    assert len(snapshot["player_game"]) == PLAYER_GAMES
    assert {name: len(rows) for name, rows in snapshot.items()} == parsed.rows

    clock.advance(timedelta(hours=1))
    second = load_dates(clean_db, store, clock, [DAY])

    assert [(r.status, r.changed) for r in second] == [(RunStatus.SUCCEEDED, 0)] * len(ROW_MODELS)
    assert _snapshot(clean_db, store, DAY) == snapshot
    # One `load_log` row per table, refreshed by the second load.
    logs = _logs(clean_db, DAY)
    assert sorted(log.table_name for log in logs) == sorted(ROW_MODELS)
    for log in logs:
        assert (log.step, log.status, log.started_at) == ("load", "succeeded", clock.now())
        assert log.detail == {"rows": parsed.rows[log.table_name], "changed": 0}


def test_a_changed_row_is_updated_in_place(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    load_dates(clean_db, store, clock, [DAY])
    rows = [PlayerGameRow.model_validate(r) for r in store.read("player_game", DAY).to_pylist()]
    edited = rows[0].model_copy(update={"pts": rows[0].pts + 1})
    store.write_rows("player_game", DAY, [edited, *rows[1:]])

    [result] = [r for r in load_dates(clean_db, store, clock, [DAY]) if r.table == "player_game"]

    assert (result.rows, result.changed) == (PLAYER_GAMES, 1)
    with clean_db() as session:
        found = session.get_one(PlayerGame, (GAME, edited.player_id))
        assert found.pts == edited.pts


def test_a_load_that_fails_rolls_back_and_records_why(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    # Box score rows of a game with no `game` row break `player_game`'s foreign key.
    result = load_table(clean_db, store, clock, "player_game", DAY)

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("IntegrityError")
    with clean_db() as session:
        count = select(func.count()).select_from(PlayerGame).where(PlayerGame.game_id == GAME)
        assert session.scalar(count) == 0
    [log] = _logs(clean_db, DAY)
    assert (log.table_name, log.status) == ("player_game", "failed")
    assert log.detail == {"error": result.error}
    with pytest.raises(LoadError, match=r"load failed: player_game 2025-10-23: IntegrityError"):
        raise_for_failures([result])


def test_a_date_never_parsed_fails_to_load(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock
) -> None:
    [game, *_] = load_dates(clean_db, store, clock, [DAY])

    assert game.status is RunStatus.FAILED
    assert game.error is not None
    assert game.error.startswith("FileNotFoundError")


def test_missing_reloads_only_what_has_no_successful_load(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    load_dates(clean_db, store, clock, DATES)
    with clean_db.begin() as session:
        session.execute(
            delete(LoadLog).where(LoadLog.table_name == "game", LoadLog.game_date == DAY_BEFORE)
        )
    clock.advance(timedelta(hours=1))

    results = load_dates(clean_db, store, clock, missing=True)

    assert results == [LoadResult("game", DAY_BEFORE, RunStatus.SUCCEEDED, 0, 0)]
    assert {log.table_name: log.started_at for log in _logs(clean_db, DAY_BEFORE)}["game"] == (
        clock.now()
    )
    assert {log.started_at for log in _logs(clean_db, DAY)} == {START}

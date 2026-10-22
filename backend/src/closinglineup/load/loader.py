"""Parquet → Postgres, idempotent per (table, game date) (spec §3.2, §6.4 `load_log`).

Each load is one transaction: `COPY` the date's parquet rows into a temporary staging table with
the target's row columns, upsert them into the target on its natural key, and upsert the
(table, game date, `load`) row of `load_log`. The upsert updates only rows whose values changed,
so reloading an unchanged file writes nothing, `updated_at` included. A load never deletes: a row
the file no longer holds stays in place.

A load that fails rolls back whole and records its `load_log` row as failed, which `load_dates`
with `missing` picks up on the next run. `load_log` is keyed by (table, game date, step), so a
rerun refreshes that row rather than adding another.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

import psycopg
from psycopg import sql
from sqlalchemy import column, func, select, table, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import Clock
from closinglineup.db.models import Base, LoadLog, RunStatus
from closinglineup.load.parquet import ROW_MODELS, ParquetStore

LOAD_STEP = "load"

# The unique key each table's rows are upserted on (spec §6: natural keys are unique constraints).
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "game": ("game_id",),
    "player": ("nba_player_id",),
    "roster_membership": ("player_id", "team_id", "start_date"),
    "player_game": ("game_id", "player_id"),
    "matchup": ("game_id", "off_player_id", "def_player_id"),
}


class LoadError(RuntimeError):
    """One or more tables of a date failed to load."""


@dataclass(frozen=True, slots=True)
class LoadResult:
    """One (table, game date) load: how many rows the file held, how many of them inserted or
    changed a row, or why it failed."""

    table: str
    game_date: date
    status: RunStatus
    rows: int = 0
    changed: int = 0
    error: str | None = None


def _upsert(
    session: Session, store: ParquetStore, table_name: str, game_date: date
) -> tuple[int, int]:
    data = store.read(table_name, game_date)
    columns = data.column_names
    staging = f"staging_{table_name}"
    column_list = sql.SQL(", ").join(map(sql.Identifier, columns))
    # The ORM session's own connection, so the COPY is in the load's transaction.
    raw = cast(psycopg.Connection[Any], session.connection().connection.driver_connection)
    with raw.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT {} FROM {} WITH NO DATA").format(
                sql.Identifier(staging), column_list, sql.Identifier(table_name)
            )
        )
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(staging), column_list)
        with cursor.copy(copy_sql) as copy:
            for values in zip(*(data.column(name).to_pylist() for name in columns), strict=True):
                copy.write_row(values)

    target = Base.metadata.tables[table_name]
    key = NATURAL_KEYS[table_name]
    updated = [name for name in columns if name not in key]
    staged = table(staging, *(column(name, target.c[name].type) for name in columns))
    stmt = insert(target).from_select(columns, select(*staged.c))
    stmt = stmt.on_conflict_do_update(
        index_elements=key,
        set_={**{name: stmt.excluded[name] for name in updated}, "updated_at": func.now()},
        where=tuple_(*(target.c[name] for name in updated)).is_distinct_from(
            tuple_(*(stmt.excluded[name] for name in updated))
        ),
    )
    # Counted from RETURNING: a Core INSERT through psycopg reports a `rowcount` of -1. Rows the
    # WHERE skips are not returned.
    changed = len(session.execute(stmt.returning(target.c[key[0]])).all())
    return data.num_rows, changed


def _log(
    session: Session,
    table_name: str,
    game_date: date,
    status: RunStatus,
    started_at: datetime,
    finished_at: datetime,
    detail: dict[str, Any],
) -> None:
    values = {
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "detail": detail,
    }
    stmt = insert(LoadLog).values(
        table_name=table_name, game_date=game_date, step=LOAD_STEP, **values
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["table_name", "game_date", "step"],
            set_={**values, "updated_at": func.now()},
        )
    )


def load_table(
    db: sessionmaker[Session], store: ParquetStore, clock: Clock, table_name: str, game_date: date
) -> LoadResult:
    """Load one table's file for one date, and record the load in `load_log`.

    A missing or stale file, or a database error, fails the load rather than raising; a database
    too far gone to record the failure raises.
    """
    started_at = clock.now()
    try:
        with db.begin() as session:
            rows, changed = _upsert(session, store, table_name, game_date)
            detail = {"rows": rows, "changed": changed}
            _log(
                session, table_name, game_date, RunStatus.SUCCEEDED, started_at, clock.now(), detail
            )
    except (OSError, ValueError, SQLAlchemyError, psycopg.Error) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        with db.begin() as session:
            _log(
                session,
                table_name,
                game_date,
                RunStatus.FAILED,
                started_at,
                clock.now(),
                {"error": reason},
            )
        return LoadResult(table_name, game_date, RunStatus.FAILED, error=reason)
    return LoadResult(table_name, game_date, RunStatus.SUCCEEDED, rows, changed)


def _loaded(db: sessionmaker[Session], dates: list[date]) -> set[tuple[str, date]]:
    """The (table, game date) pairs among `dates` whose last load succeeded."""
    with db() as session:
        found = session.execute(
            select(LoadLog.table_name, LoadLog.game_date).where(
                LoadLog.step == LOAD_STEP,
                LoadLog.status == RunStatus.SUCCEEDED,
                LoadLog.game_date.in_(dates),
            )
        )
        return {(name, day) for name, day in found}


def load_dates(
    db: sessionmaker[Session],
    store: ParquetStore,
    clock: Clock,
    dates: Iterable[date] | None = None,
    *,
    missing: bool = False,
) -> list[LoadResult]:
    """Load every table of each date, date by date in order; with no `dates`, every date the
    store holds a file for. `missing` skips the (table, date) pairs whose last load succeeded."""
    days = sorted(set(dates)) if dates is not None else store.dates()
    pairs = [(name, day) for day in days for name in ROW_MODELS]
    if missing:
        done = _loaded(db, days)
        pairs = [pair for pair in pairs if pair not in done]
    return [load_table(db, store, clock, name, day) for name, day in pairs]


def raise_for_failures(results: Iterable[LoadResult]) -> None:
    """Raise `LoadError` naming every failed load in `results`, if any failed."""
    if failed := [r for r in results if r.status is RunStatus.FAILED]:
        listed = "; ".join(f"{r.table} {r.game_date}: {r.error}" for r in failed)
        raise LoadError(f"load failed: {listed}")

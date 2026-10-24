"""Parquet → Postgres, idempotent per file (spec §3.2, §6.4 `load_log`).

Each load is one transaction: `COPY` the file's parquet rows into a temporary staging table with
the target's row columns, upsert them into the target on its natural key, and for a game date's
file, upsert the (table, game date, `load`) row of `load_log`. The upsert updates only rows whose
values changed, so reloading an unchanged file writes nothing, `updated_at` included.

A load deletes only from the tables in `REPLACE_SCOPES`, whose natural keys a re-parse can move:
a stint's start or a possession's number shifts when an override changes how pbpstats reads a
game, a shot can be credited to another player, and a game's box score re-parsed from
stats.nba.com in place of cdn.nba.com's live one (`pipeline`) need not list the same players.
Their files replace what was loaded before, within the scope of the rows they hold: every game a
box score, stint, possession or possession lineup file covers, and every player and season type a
shot file covers. A row in that scope the file no
longer holds is deleted. Every other table keeps a row its file no longer holds.

A date's load that fails rolls back whole and records its `load_log` row as failed, which
`load_dates` with `missing` picks up on the next run. `load_log` is keyed by (table, game date,
step), so a rerun refreshes that row rather than adding another. A `shot` file has no game date
and no `load_log` row: `load_keyed` reloads every file, which writes nothing for those unchanged.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

import psycopg
from psycopg import sql
from sqlalchemy import ColumnElement, column, delete, func, select, table, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import Clock
from closinglineup.db.models import Base, LoadLog, RunStatus
from closinglineup.load.parquet import ROW_MODELS, FileKey, ParquetStore

LOAD_STEP = "load"

# The unique key each table's rows are upserted on (spec §6: natural keys are unique constraints).
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "game": ("game_id",),
    "player": ("nba_player_id",),
    "roster_membership": ("player_id", "team_id", "start_date"),
    "player_game": ("game_id", "player_id"),
    "matchup": ("game_id", "off_player_id", "def_player_id"),
    "stint": ("game_id", "team_id", "period", "start_sec"),
    "possession": ("game_id", "period", "number"),
    "possession_lineup": ("game_id", "period", "number", "team_id", "lineup_key"),
    "shot": ("game_id", "event_num"),
}

# A game ID's first five characters, `00` + season type + season year (`season.py`): the season
# and season type a shot chart covers.
SEASON_TYPE_PREFIX = 5

type Columns = Callable[[str], ColumnElement[Any]]

# The tables a load replaces within a scope, by the expressions that scope it: a target row
# whose scope matches a staged row's and whose natural key no staged row has is deleted.
REPLACE_SCOPES: dict[str, Callable[[Columns], tuple[ColumnElement[Any], ...]]] = {
    "player_game": lambda c: (c("game_id"),),
    "stint": lambda c: (c("game_id"),),
    "possession": lambda c: (c("game_id"),),
    "possession_lineup": lambda c: (c("game_id"),),
    "shot": lambda c: (c("player_id"), func.substr(c("game_id"), 1, SEASON_TYPE_PREFIX)),
}


class LoadError(RuntimeError):
    """One or more tables of a date failed to load."""


@dataclass(frozen=True, slots=True)
class LoadResult:
    """One file's load: how many rows the file held, how many of them inserted or changed a row,
    how many rows it removed (`REPLACE_SCOPES`), or why it failed."""

    table: str
    key: FileKey
    status: RunStatus
    rows: int = 0
    changed: int = 0
    removed: int = 0
    error: str | None = None


def _upsert(
    session: Session, store: ParquetStore, table_name: str, key: FileKey
) -> tuple[int, int, int]:
    data = store.read(table_name, key)
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
    natural = NATURAL_KEYS[table_name]
    updated = [name for name in columns if name not in natural]
    staged = table(staging, *(column(name, target.c[name].type) for name in columns))
    stmt = insert(target).from_select(columns, select(*staged.c))
    if updated:
        stmt = stmt.on_conflict_do_update(
            index_elements=natural,
            set_={**{name: stmt.excluded[name] for name in updated}, "updated_at": func.now()},
            where=tuple_(*(target.c[name] for name in updated)).is_distinct_from(
                tuple_(*(stmt.excluded[name] for name in updated))
            ),
        )
    else:
        # Every column is the key (`possession_lineup`): a row already there is unchanged.
        stmt = stmt.on_conflict_do_nothing(index_elements=natural)
    # Counted from RETURNING: a Core INSERT through psycopg reports a `rowcount` of -1. Rows the
    # WHERE or DO NOTHING skips are not returned.
    changed = len(session.execute(stmt.returning(target.c[natural[0]])).all())

    removed = 0
    if (scope := REPLACE_SCOPES.get(table_name)) is not None:
        stale = delete(target).where(
            tuple_(*scope(lambda name: target.c[name])).in_(
                select(*scope(lambda name: staged.c[name])).distinct()
            ),
            tuple_(*(target.c[name] for name in natural)).not_in(
                select(*(staged.c[name] for name in natural))
            ),
        )
        removed = len(session.execute(stale.returning(target.c[natural[0]])).all())
    return data.num_rows, changed, removed


def write_load_log(
    session: Session,
    table_name: str,
    game_date: date,
    status: RunStatus,
    started_at: datetime,
    finished_at: datetime,
    detail: dict[str, Any],
    *,
    step: str = LOAD_STEP,
) -> None:
    """Upsert the (table, game date, step) row of `load_log`; `derive.run` records its steps here
    too."""
    values = {
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "detail": detail,
    }
    stmt = insert(LoadLog).values(table_name=table_name, game_date=game_date, step=step, **values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["table_name", "game_date", "step"],
            set_={**values, "updated_at": func.now()},
        )
    )


def load_table(
    db: sessionmaker[Session], store: ParquetStore, clock: Clock, table_name: str, key: FileKey
) -> LoadResult:
    """Load one table's file for one key, and record a game date's load in `load_log`.

    A missing or stale file, or a database error, fails the load rather than raising; a database
    too far gone to record the failure raises.
    """
    started_at = clock.now()
    try:
        with db.begin() as session:
            rows, changed, removed = _upsert(session, store, table_name, key)
            detail = {"rows": rows, "changed": changed, "removed": removed}
            if isinstance(key, date):
                write_load_log(
                    session, table_name, key, RunStatus.SUCCEEDED, started_at, clock.now(), detail
                )
    except (OSError, ValueError, SQLAlchemyError, psycopg.Error) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(key, date):
            with db.begin() as session:
                write_load_log(
                    session,
                    table_name,
                    key,
                    RunStatus.FAILED,
                    started_at,
                    clock.now(),
                    {"error": reason},
                )
        return LoadResult(table_name, key, RunStatus.FAILED, error=reason)
    return LoadResult(table_name, key, RunStatus.SUCCEEDED, rows, changed, removed)


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
    store holds a file for. `missing` skips the (table, date) pairs whose last load succeeded,
    and those with no file: a date parsed before its table existed has nothing a load can heal,
    and only a re-parse writes its file."""
    days = sorted(set(dates)) if dates is not None else store.dates()
    pairs = [(name, day) for day in days for name in ROW_MODELS]
    if missing:
        done = _loaded(db, days)
        pairs = [pair for pair in pairs if pair not in done and store.path(*pair).is_file()]
    return [load_table(db, store, clock, name, day) for name, day in pairs]


def load_keyed(
    db: sessionmaker[Session],
    store: ParquetStore,
    clock: Clock,
    table_name: str,
    keys: Iterable[str] | None = None,
) -> list[LoadResult]:
    """Load each of a `KEYED_ROW_MODELS` table's files, in key order; with no `keys`, every file
    the store holds for the table."""
    found = sorted(set(keys)) if keys is not None else store.keys(table_name)
    return [load_table(db, store, clock, table_name, key) for key in found]


def raise_for_failures(results: Iterable[LoadResult]) -> None:
    """Raise `LoadError` naming every failed load in `results`, if any failed."""
    if failed := [r for r in results if r.status is RunStatus.FAILED]:
        listed = "; ".join(f"{r.table} {r.key}: {r.error}" for r in failed)
        raise LoadError(f"load failed: {listed}")

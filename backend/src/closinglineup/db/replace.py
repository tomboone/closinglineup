"""Make a table hold exactly a given set of rows, for the tables written whole from one source: the
`transaction` overlay from the full player-movement feed, and `roster_membership` from `derive`.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, Table, bindparam, delete, func, insert, select, update
from sqlalchemy.orm import Session

# Rows deleted per statement, well inside Postgres's 65,535 bind parameters.
DELETE_BATCH = 10_000


def replace_rows(
    session: Session,
    table: Table,
    natural: Sequence[str],
    rows: Sequence[dict[str, Any]],
    scope: ColumnElement[bool] | None = None,
) -> tuple[int, int]:
    """Insert each of `rows` whose natural key the table lacks, update each whose other columns
    changed, and delete every row of the table, or of its rows `scope` selects, whose natural key
    `rows` lacks; an unchanged row is not written, `updated_at` included. Returns (inserted or
    updated, deleted).

    `rows` must not repeat a natural key, and `table` must have an `id` primary key.
    """
    columns = list(rows[0]) if rows else []
    values = [name for name in columns if name not in natural]
    query = select(table.c.id, *(table.c[name] for name in (*natural, *values)))
    found = session.execute(query if scope is None else query.where(scope)).all()
    existing = {
        tuple(row[1 : len(natural) + 1]): (row[0], tuple(row[len(natural) + 1 :])) for row in found
    }
    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(row[name] for name in natural)
        if key not in existing:
            added.append(row)
            continue
        row_id, old = existing.pop(key)
        if old != tuple(row[name] for name in values):
            changed.append({"row_id": row_id, **{name: row[name] for name in values}})
    if added:
        session.execute(insert(table), added)
    if changed:
        # The SET clause is each changed row's columns, the keys of its parameters but `row_id`.
        stmt = update(table).where(table.c.id == bindparam("row_id"))
        session.execute(stmt.values(updated_at=func.now()), changed)
    stale = [row_id for row_id, _ in existing.values()]
    for start in range(0, len(stale), DELETE_BATCH):
        session.execute(delete(table).where(table.c.id.in_(stale[start : start + DELETE_BATCH])))
    return len(added) + len(changed), len(stale)

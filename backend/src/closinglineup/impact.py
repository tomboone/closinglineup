"""The `impact_snapshot` table's row, which each impact-metric overlay writes (spec §4.5, §6.3):
DARKO's DPM, EPM, and the BPM `derive` computes, each a `source` of its own; and the value metrics
`derive.value` computes, VORP and Win Shares (v2 spec §8.4)."""

from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from sqlalchemy import delete, func, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from closinglineup.db.models import Base

TABLE = "impact_snapshot"
NATURAL_KEY = ("date", "player_id", "source")
VALUES = ("value", "o_value", "d_value", "minutes_basis")

type ImpactSource = Literal["epm", "darko", "bpm_local", "ws_local", "vorp_local"]


@dataclass(frozen=True, slots=True)
class ImpactSnapshotRow:
    """One metric's value for a player on a date, as an `impact_snapshot` row's column values.
    `o_value`, `d_value` and `minutes_basis` are None where the source publishes none."""

    date: date
    player_id: int
    source: ImpactSource
    value: float
    o_value: float | None
    d_value: float | None
    minutes_basis: float | None


def replace_snapshot(
    session: Session, day: date, source: ImpactSource, rows: list[ImpactSnapshotRow]
) -> tuple[int, int]:
    """Make `day`'s rows of `source` exactly `rows`: insert each the table lacks, update each
    whose values changed, and delete each player's row `rows` lack; an unchanged row is not
    written, `updated_at` included. Returns (inserted or updated, deleted).

    `rows` must be for `day` and `source`, and must not repeat a player.

    Not `db.replace.replace_rows`, which updates through an `id` column the table does not have:
    its key is the natural one, so the upsert is Postgres's ON CONFLICT, as the loader's is.
    """
    table = Base.metadata.tables[TABLE]
    stale = delete(table).where(
        table.c.date == day,
        table.c.source == source,
        table.c.player_id.not_in([row.player_id for row in rows]),
    )
    removed = len(session.execute(stale.returning(table.c.player_id)).all())
    if not rows:
        return 0, removed
    stmt = insert(table).values([asdict(row) for row in rows])
    stmt = stmt.on_conflict_do_update(
        index_elements=NATURAL_KEY,
        set_={**{name: stmt.excluded[name] for name in VALUES}, "updated_at": func.now()},
        where=tuple_(*(table.c[name] for name in VALUES)).is_distinct_from(
            tuple_(*(stmt.excluded[name] for name in VALUES))
        ),
    )
    # Counted from RETURNING, as the loader does: rows the WHERE skips are not returned.
    changed = len(session.execute(stmt.returning(table.c.player_id)).all())
    return changed, removed

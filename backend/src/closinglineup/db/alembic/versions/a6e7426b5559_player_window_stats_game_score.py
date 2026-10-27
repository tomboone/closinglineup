"""player_window_stats game_score

Revision ID: a6e7426b5559
Revises: a3f1c2d4e5b6
Create Date: 2026-10-27 09:00:00.000000

`player_window_stats`' production rows gain Hollinger's Game Score per game played, with its
deltas (NEU-1995, v2 spec §8.1). The view is dropped and recreated from the copy of `db/views.sql`
beside this file, as `869602e50229` created it; downgrading recreates that revision's view.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "a6e7426b5559"
down_revision: str | Sequence[str] | None = "a3f1c2d4e5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW_SQL = Path(__file__).with_suffix(".sql")
PREVIOUS_VIEW_SQL = Path(__file__).with_name("869602e50229_player_window_stats.sql")


def _create_view(sql: Path) -> None:
    # As `869602e50229` does: JIT off, since compiling the view's query takes longer than running
    # it, and a bare DB-API cursor, since the file is several statements and its `%` signs are not
    # placeholders.
    op.execute("SET LOCAL jit = off")
    cursor = op.get_bind().connection.cursor()
    try:
        cursor.execute(sql.read_text())
    finally:
        cursor.close()
    op.execute("RESET jit")


def upgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW player_window_stats")
    _create_view(VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW player_window_stats")
    _create_view(PREVIOUS_VIEW_SQL)

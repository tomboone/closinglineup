"""player_window_stats windowed Win Shares

Revision ID: d2a9f3b8e614
Revises: c7e19a4d2b30
Create Date: 2026-10-27 18:00:00.000000

`player_window_stats`' production rows carry Win Shares over the window, `ws_window` with its
offensive and defensive parts, and WS/48, `ws_48`, with its deltas (NEU-2003, v2 spec §15.1). The
view is dropped and recreated from the copy of `db/views.sql` beside this file, as `c7e19a4d2b30`
recreated it; downgrading recreates that revision's view.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "d2a9f3b8e614"
down_revision: str | Sequence[str] | None = "c7e19a4d2b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW_SQL = Path(__file__).with_suffix(".sql")
PREVIOUS_VIEW_SQL = Path(__file__).with_name("c7e19a4d2b30_player_window_stats_value_snapshots.sql")


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

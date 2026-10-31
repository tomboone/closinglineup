"""player_window_stats value snapshots

Revision ID: c7e19a4d2b30
Revises: b4d8e2f1c907
Create Date: 2026-10-27 15:00:00.000000

`player_window_stats`' production rows carry the latest Win Shares and VORP snapshots, `ws_local`
with its offensive and defensive parts and `vorp_local`, within the date's season; the prior_season
row carries the prior season's final snapshot (NEU-1998, v2 spec §8.4). The view is dropped and
recreated from the copy of `db/views.sql` beside this file, as `a6e7426b5559` recreated it;
downgrading recreates that revision's view.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "c7e19a4d2b30"
down_revision: str | Sequence[str] | None = "b4d8e2f1c907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW_SQL = Path(__file__).with_suffix(".sql")
PREVIOUS_VIEW_SQL = Path(__file__).with_name("a6e7426b5559_player_window_stats_game_score.sql")


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

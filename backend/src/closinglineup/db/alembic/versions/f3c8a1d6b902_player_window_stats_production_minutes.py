"""player_window_stats production games played and minutes per game

Revision ID: f3c8a1d6b902
Revises: d2a9f3b8e614
Create Date: 2026-10-27 20:00:00.000000

`player_window_stats`' production rows carry `games_played` and `minutes_per_game` over the
player's games whatever his team, so the league table's columns all cover the same games
(NEU-2006). The view is dropped and recreated from the copy of `db/views.sql` beside this file, as
`d2a9f3b8e614` recreated it; downgrading recreates that revision's view.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "f3c8a1d6b902"
down_revision: str | Sequence[str] | None = "d2a9f3b8e614"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW_SQL = Path(__file__).with_suffix(".sql")
PREVIOUS_VIEW_SQL = Path(__file__).with_name(
    "d2a9f3b8e614_player_window_stats_windowed_win_shares.sql"
)


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

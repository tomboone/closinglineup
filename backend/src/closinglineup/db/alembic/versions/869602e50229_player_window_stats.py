"""player_window_stats

Revision ID: 869602e50229
Revises: e347b161377e
Create Date: 2026-10-23 21:10:00.000000

The materialized view of every window metric (NEU-1875, spec §6.4, ADR-0003), created from the
copy of `db/views.sql` beside this file rather than from `db/views.sql` itself, so replaying
history builds the view this revision defined against the tables as they stood here. A later
change to the view is a new migration with a copy of its own; `tests/test_windows.py` holds the
newest copy to the file.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "869602e50229"
down_revision: str | Sequence[str] | None = "e347b161377e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW_SQL = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    # JIT compiling the view's query takes longer than running it (`derive.windows`).
    op.execute("SET LOCAL jit = off")
    # A bare DB-API cursor with no parameters: the file is several statements, and neither its
    # `%` signs nor anything like a `:name` is a placeholder.
    cursor = op.get_bind().connection.cursor()
    try:
        cursor.execute(VIEW_SQL.read_text())
    finally:
        cursor.close()
    op.execute("RESET jit")


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW player_window_stats")

"""game tip_time

Revision ID: a3f1c2d4e5b6
Revises: 7c1e4b9d2a60
Create Date: 2026-10-26 19:00:00.000000

`game` gains its tip-off from the schedule's `gameDateTimeEst` (NEU-1983, v2 spec §5.5): NULL for
a game with no time listed, and for every game until a parse or the nightly schedule load fills it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f1c2d4e5b6"
down_revision: str | Sequence[str] | None = "7c1e4b9d2a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("game", sa.Column("tip_time", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("game", "tip_time")

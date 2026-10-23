"""possession_lineup

Revision ID: 5afc3499972a
Revises: 5ba1d31c8e6a
Create Date: 2026-10-23 02:32:14.002798
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5afc3499972a"
down_revision: str | Sequence[str] | None = "5ba1d31c8e6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "possession_lineup",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("lineup_key", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"], ["game.game_id"], name=op.f("fk_possession_lineup_game_id_game")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_possession_lineup_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_possession_lineup")),
        sa.UniqueConstraint(
            "game_id",
            "period",
            "number",
            "team_id",
            "lineup_key",
            name=op.f("uq_possession_lineup_game_id_period_number_team_id_lineup_key"),
        ),
    )


def downgrade() -> None:
    op.drop_table("possession_lineup")

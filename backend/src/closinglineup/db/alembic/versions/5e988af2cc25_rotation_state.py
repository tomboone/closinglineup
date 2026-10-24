"""rotation_state

Revision ID: 5e988af2cc25
Revises: 869602e50229
Create Date: 2026-10-24 00:30:26.141972

One row per player-team-game-date, its rotation tier and the inputs the rule read (NEU-1876,
spec §6.4, §7.5, ADR-0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5e988af2cc25"
down_revision: str | Sequence[str] | None = "869602e50229"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `sa.Double[float]()` where autogenerate writes `sa.Double()`, which pyright strict cannot type.
def upgrade() -> None:
    op.create_table(
        "rotation_state",
        sa.Column("player_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("minutes_share_10", sa.Double[float](), nullable=True),
        sa.Column("starter_rate_5", sa.Double[float](), nullable=True),
        sa.Column("played_rate_10", sa.Double[float](), nullable=True),
        sa.Column("availability", sa.String(), nullable=False),
        sa.Column("badges", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("is_short", sa.Boolean(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint(
            "availability IN ('active', 'out', 'doubtful', 'questionable', 'probable', "
            "'available', 'inactive', 'suspended', 'g_league')",
            name=op.f("ck_rotation_state_availability"),
        ),
        sa.CheckConstraint(
            "tier IN ('injured', 'starter', 'core', 'fringe', 'out')",
            name=op.f("ck_rotation_state_tier"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_rotation_state_team_id_team")
        ),
        sa.PrimaryKeyConstraint(
            "player_id", "team_id", "game_date", name=op.f("pk_rotation_state")
        ),
    )
    op.create_index(
        op.f("ix_rotation_state_team_id_game_date"),
        "rotation_state",
        ["team_id", "game_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rotation_state_team_id_game_date"), table_name="rotation_state")
    op.drop_table("rotation_state")

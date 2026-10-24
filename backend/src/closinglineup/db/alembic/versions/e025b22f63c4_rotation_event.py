"""rotation_event

Revision ID: e025b22f63c4
Revises: 5e988af2cc25
Create Date: 2026-10-24 00:49:48.131813

One row per change in a player-team's rotation tier between its game dates, or arrival on the
team, with its top three contributing signals (NEU-1877, spec §6.4, §7.6, ADR-0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e025b22f63c4"
down_revision: str | Sequence[str] | None = "5e988af2cc25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rotation_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("prev_game_date", sa.Date(), nullable=True),
        sa.Column("from_tier", sa.String(), nullable=True),
        sa.Column("to_tier", sa.String(), nullable=False),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "from_tier IN ('injured', 'starter', 'core', 'fringe', 'out')",
            name=op.f("ck_rotation_event_from_tier"),
        ),
        sa.CheckConstraint(
            "to_tier IN ('injured', 'starter', 'core', 'fringe', 'out')",
            name=op.f("ck_rotation_event_to_tier"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_rotation_event_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rotation_event")),
        sa.UniqueConstraint(
            "player_id",
            "team_id",
            "game_date",
            name=op.f("uq_rotation_event_player_id_team_id_game_date"),
        ),
    )
    op.create_index(
        op.f("ix_rotation_event_game_date"), "rotation_event", ["game_date"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rotation_event_game_date"), table_name="rotation_event")
    op.drop_table("rotation_event")

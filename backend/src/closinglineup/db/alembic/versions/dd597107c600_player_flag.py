"""player_flag

Revision ID: dd597107c600
Revises: e025b22f63c4
Create Date: 2026-10-24 01:14:30.753080

One row per flag raised on a player-team on one of its game dates, with the numbers behind it
(NEU-1878, spec §6.4, §7.7, ADR-0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "dd597107c600"
down_revision: str | Sequence[str] | None = "e025b22f63c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_flag",
        sa.Column("player_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("flag", sa.String(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "flag IN ('minutes_cliff', 'usage_up_efficiency_down', 'bleeding', 'earning_trust', "
            "'yo_yo', 'contract_pressure')",
            name=op.f("ck_player_flag_flag"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_player_flag_team_id_team")
        ),
        sa.PrimaryKeyConstraint(
            "player_id", "team_id", "game_date", "flag", name=op.f("pk_player_flag")
        ),
    )
    op.create_index(op.f("ix_player_flag_game_date"), "player_flag", ["game_date"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_player_flag_game_date"), table_name="player_flag")
    op.drop_table("player_flag")

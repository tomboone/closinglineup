"""snapshot tables

Revision ID: ce031dc8c539
Revises: 5afc3499972a
Create Date: 2026-10-23 13:27:16.960579
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ce031dc8c539"
down_revision: str | Sequence[str] | None = "5afc3499972a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `sa.Double[float]()` where autogenerate writes `sa.Double()`, which pyright strict cannot type.
def upgrade() -> None:
    op.create_table(
        "contract",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.String(), nullable=False),
        sa.Column("salary", sa.Integer(), nullable=False),
        sa.Column("guaranteed", sa.Integer(), nullable=False),
        sa.Column("option_type", sa.String(), nullable=True),
        sa.Column("option_date", sa.Date(), nullable=True),
        sa.Column("trade_eligible_date", sa.Date(), nullable=True),
        sa.Column("two_way", sa.Boolean(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
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
            "option_type IN ('player', 'team', 'early_termination')",
            name=op.f("ck_contract_option_type"),
        ),
        sa.CheckConstraint("source IN ('spotrac', 'realgm')", name=op.f("ck_contract_source")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contract")),
        sa.UniqueConstraint(
            "player_id",
            "season",
            "as_of_date",
            "source",
            name=op.f("uq_contract_player_id_season_as_of_date_source"),
        ),
    )
    op.create_table(
        "impact_snapshot",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("player_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("value", sa.Double[float](), nullable=False),
        sa.Column("o_value", sa.Double[float](), nullable=True),
        sa.Column("d_value", sa.Double[float](), nullable=True),
        sa.Column("minutes_basis", sa.Double[float](), nullable=True),
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
            "source IN ('epm', 'darko', 'bpm_local')", name=op.f("ck_impact_snapshot_source")
        ),
        sa.PrimaryKeyConstraint("date", "player_id", "source", name=op.f("pk_impact_snapshot")),
    )
    op.create_table(
        "transaction",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("from_team_id", sa.Integer(), nullable=True),
        sa.Column("to_team_id", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(), nullable=False),
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
            "type IN ('sign', 'waive', 'trade', 'two_way_sign', 'two_way_convert', 'assign', "
            "'recall', 'option_exercise', 'option_decline', 'other')",
            name=op.f("ck_transaction_type"),
        ),
        sa.ForeignKeyConstraint(
            ["from_team_id"], ["team.nba_team_id"], name=op.f("fk_transaction_from_team_id_team")
        ),
        sa.ForeignKeyConstraint(
            ["to_team_id"], ["team.nba_team_id"], name=op.f("fk_transaction_to_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transaction")),
        sa.UniqueConstraint(
            "player_id",
            "date",
            "type",
            "detail",
            name=op.f("uq_transaction_player_id_date_type_detail"),
        ),
    )
    op.create_table(
        "injury_status",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("report_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("game_id", sa.String(), nullable=True),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
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
        sa.CheckConstraint("source IN ('pdf', 'espn')", name=op.f("ck_injury_status_source")),
        sa.CheckConstraint(
            "status IN ('Out', 'Doubtful', 'Questionable', 'Probable', 'Available')",
            name=op.f("ck_injury_status_status"),
        ),
        sa.ForeignKeyConstraint(
            ["game_id"], ["game.game_id"], name=op.f("fk_injury_status_game_id_game")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_injury_status_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_injury_status")),
        sa.UniqueConstraint(
            "player_id",
            "game_date",
            "report_datetime",
            "source",
            name=op.f("uq_injury_status_player_id_game_date_report_datetime_source"),
        ),
    )
    op.create_index(
        op.f("ix_injury_status_team_id_report_datetime"),
        "injury_status",
        ["team_id", "report_datetime"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_injury_status_team_id_report_datetime"), table_name="injury_status")
    op.drop_table("injury_status")
    op.drop_table("transaction")
    op.drop_table("impact_snapshot")
    op.drop_table("contract")

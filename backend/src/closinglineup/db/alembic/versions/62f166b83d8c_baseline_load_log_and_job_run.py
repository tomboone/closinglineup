"""baseline load_log and job_run

Revision ID: 62f166b83d8c
Revises:
Create Date: 2026-10-21 21:09:25.989645
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "62f166b83d8c"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "status IN ('running', 'succeeded', 'failed')", name=op.f("ck_job_run_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_run")),
    )
    op.create_index(op.f("ix_job_run_job"), "job_run", ["job"], unique=False)
    op.create_table(
        "load_log",
        sa.Column("table_name", sa.String(), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("step", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "status IN ('running', 'succeeded', 'failed')", name=op.f("ck_load_log_status")
        ),
        sa.PrimaryKeyConstraint("table_name", "game_date", "step", name=op.f("pk_load_log")),
    )


def downgrade() -> None:
    op.drop_table("load_log")
    op.drop_index(op.f("ix_job_run_job"), table_name="job_run")
    op.drop_table("job_run")

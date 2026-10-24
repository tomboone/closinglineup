"""load_log drift status

Revision ID: 7c1e4b9d2a60
Revises: dd597107c600
Create Date: 2026-10-24 12:00:00.000000

`load_log` records a fetched response whose shape has drifted from its endpoint's with status
`drift` (NEU-1891, `sources.drift`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "7c1e4b9d2a60"
down_revision: str | Sequence[str] | None = "dd597107c600"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_load_log_status"), "load_log", type_="check")
    op.create_check_constraint(
        op.f("ck_load_log_status"),
        "load_log",
        "status IN ('running', 'succeeded', 'failed', 'drift')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM load_log WHERE status = 'drift'")
    op.drop_constraint(op.f("ck_load_log_status"), "load_log", type_="check")
    op.create_check_constraint(
        op.f("ck_load_log_status"), "load_log", "status IN ('running', 'succeeded', 'failed')"
    )

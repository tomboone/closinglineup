"""impact_snapshot value sources

Revision ID: b4d8e2f1c907
Revises: a6e7426b5559
Create Date: 2026-10-27 12:00:00.000000

`impact_snapshot` holds the value metrics `derive.value` computes beside the impact ratings: Win
Shares as `ws_local` and VORP as `vorp_local` (NEU-1996, v2 spec §8.4).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4d8e2f1c907"
down_revision: str | Sequence[str] | None = "a6e7426b5559"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_impact_snapshot_source"), "impact_snapshot", type_="check")
    op.create_check_constraint(
        op.f("ck_impact_snapshot_source"),
        "impact_snapshot",
        "source IN ('epm', 'darko', 'bpm_local', 'ws_local', 'vorp_local')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM impact_snapshot WHERE source IN ('ws_local', 'vorp_local')")
    op.drop_constraint(op.f("ck_impact_snapshot_source"), "impact_snapshot", type_="check")
    op.create_check_constraint(
        op.f("ck_impact_snapshot_source"),
        "impact_snapshot",
        "source IN ('epm', 'darko', 'bpm_local')",
    )

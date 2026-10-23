"""contract from Hoops Rumors

Revision ID: e347b161377e
Revises: ce031dc8c539
Create Date: 2026-10-23 18:40:00.000000

`contract` narrows to non-guaranteed contracts from Hoops Rumors (NEU-1873, ADR-0004): no
options, no trade-eligible date, no two-way flag, and a NULL salary for a minimum-salary contract
whose amount no source gives. Nothing has written the table, so the NOT NULL columns are added
without a default.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e347b161377e"
down_revision: str | Sequence[str] | None = "ce031dc8c539"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_contract_option_type"), "contract", type_="check")
    op.drop_constraint(op.f("ck_contract_source"), "contract", type_="check")
    op.drop_column("contract", "option_type")
    op.drop_column("contract", "option_date")
    op.drop_column("contract", "trade_eligible_date")
    op.drop_column("contract", "two_way")
    op.alter_column("contract", "salary", existing_type=sa.Integer(), nullable=True)
    op.add_column("contract", sa.Column("minimum_salary", sa.Boolean(), nullable=False))
    op.add_column("contract", sa.Column("guarantee_date", sa.Date(), nullable=False))
    op.add_column("contract", sa.Column("guaranteed_after", sa.Integer(), nullable=True))
    op.create_check_constraint(op.f("ck_contract_source"), "contract", "source IN ('hoopsrumors')")


def downgrade() -> None:
    # The earlier shape cannot hold a Hoops Rumors row: its source and NOT NULL columns refuse it.
    op.execute("DELETE FROM contract")
    op.drop_constraint(op.f("ck_contract_source"), "contract", type_="check")
    op.drop_column("contract", "guaranteed_after")
    op.drop_column("contract", "guarantee_date")
    op.drop_column("contract", "minimum_salary")
    op.alter_column("contract", "salary", existing_type=sa.Integer(), nullable=False)
    op.add_column("contract", sa.Column("two_way", sa.Boolean(), nullable=False))
    op.add_column("contract", sa.Column("trade_eligible_date", sa.Date(), nullable=True))
    op.add_column("contract", sa.Column("option_date", sa.Date(), nullable=True))
    op.add_column("contract", sa.Column("option_type", sa.String(), nullable=True))
    op.create_check_constraint(
        op.f("ck_contract_source"), "contract", "source IN ('spotrac', 'realgm')"
    )
    op.create_check_constraint(
        op.f("ck_contract_option_type"),
        "contract",
        "option_type IN ('player', 'team', 'early_termination')",
    )

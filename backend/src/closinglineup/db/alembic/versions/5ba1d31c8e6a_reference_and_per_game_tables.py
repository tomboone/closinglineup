"""reference and per-game tables

Revision ID: 5ba1d31c8e6a
Revises: 62f166b83d8c
Create Date: 2026-10-22 03:43:53.252798
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from closinglineup.teams import TEAMS

revision: str = "5ba1d31c8e6a"
down_revision: str | Sequence[str] | None = "62f166b83d8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `sa.Double[float]()` where autogenerate writes `sa.Double()`, which pyright strict cannot type.
def upgrade() -> None:
    op.create_table(
        "player",
        sa.Column("nba_player_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("birthdate", sa.Date(), nullable=True),
        sa.Column("draft_year", sa.Integer(), nullable=True),
        sa.Column("draft_pick", sa.Integer(), nullable=True),
        sa.Column("height_in", sa.Integer(), nullable=True),
        sa.Column("position", sa.String(), nullable=True),
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
        sa.PrimaryKeyConstraint("nba_player_id", name=op.f("pk_player")),
    )
    team = op.create_table(
        "team",
        sa.Column("nba_team_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("tricode", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("conference", sa.String(), nullable=False),
        sa.Column("division", sa.String(), nullable=False),
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
        sa.CheckConstraint("conference IN ('East', 'West')", name=op.f("ck_team_conference")),
        sa.CheckConstraint(
            "division IN ('Atlantic', 'Central', 'Southeast', 'Northwest', 'Pacific', 'Southwest')",
            name=op.f("ck_team_division"),
        ),
        sa.PrimaryKeyConstraint("nba_team_id", name=op.f("pk_team")),
        sa.UniqueConstraint("tricode", name=op.f("uq_team_tricode")),
    )
    op.bulk_insert(
        team,
        [
            {
                "nba_team_id": t.nba_team_id,
                "tricode": t.tricode,
                "name": t.name,
                "conference": t.conference,
                "division": t.division,
            }
            for t in TEAMS
        ],
    )
    op.create_table(
        "game",
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("season", sa.String(), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("home_team_id", sa.Integer(), nullable=False),
        sa.Column("away_team_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("home_b2b", sa.Boolean(), nullable=False),
        sa.Column("away_b2b", sa.Boolean(), nullable=False),
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
            "phase IN ('regular', 'cup_knockout', 'play_in', 'playoff')", name=op.f("ck_game_phase")
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'final', 'postponed')", name=op.f("ck_game_status")
        ),
        sa.ForeignKeyConstraint(
            ["away_team_id"], ["team.nba_team_id"], name=op.f("fk_game_away_team_id_team")
        ),
        sa.ForeignKeyConstraint(
            ["home_team_id"], ["team.nba_team_id"], name=op.f("fk_game_home_team_id_team")
        ),
        sa.PrimaryKeyConstraint("game_id", name=op.f("pk_game")),
    )
    op.create_index(op.f("ix_game_game_date"), "game", ["game_date"], unique=False)
    op.create_table(
        "roster_membership",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("contract_type", sa.String(), nullable=False),
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
            "contract_type IN ('standard', 'two_way', 'ten_day', 'exhibit_10')",
            name=op.f("ck_roster_membership_contract_type"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_roster_membership_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roster_membership")),
        sa.UniqueConstraint(
            "player_id",
            "team_id",
            "start_date",
            name=op.f("uq_roster_membership_player_id_team_id_start_date"),
        ),
    )
    op.create_table(
        "matchup",
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("off_player_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("def_player_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("partial_poss", sa.Double[float](), nullable=False),
        sa.Column("pts", sa.Integer(), nullable=False),
        sa.Column("fgm", sa.Integer(), nullable=False),
        sa.Column("fga", sa.Integer(), nullable=False),
        sa.Column("fg3m", sa.Integer(), nullable=False),
        sa.Column("fg3a", sa.Integer(), nullable=False),
        sa.Column("tov", sa.Integer(), nullable=False),
        sa.Column("ast", sa.Integer(), nullable=False),
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
            ["game_id"], ["game.game_id"], name=op.f("fk_matchup_game_id_game")
        ),
        sa.PrimaryKeyConstraint(
            "game_id", "off_player_id", "def_player_id", name=op.f("pk_matchup")
        ),
    )
    op.create_table(
        "player_game",
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("player_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("is_starter", sa.Boolean(), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("dnp_reason", sa.String(), nullable=True),
        sa.Column("pts", sa.Integer(), nullable=False),
        sa.Column("fgm", sa.Integer(), nullable=False),
        sa.Column("fga", sa.Integer(), nullable=False),
        sa.Column("fg3m", sa.Integer(), nullable=False),
        sa.Column("fg3a", sa.Integer(), nullable=False),
        sa.Column("ftm", sa.Integer(), nullable=False),
        sa.Column("fta", sa.Integer(), nullable=False),
        sa.Column("oreb", sa.Integer(), nullable=False),
        sa.Column("dreb", sa.Integer(), nullable=False),
        sa.Column("ast", sa.Integer(), nullable=False),
        sa.Column("stl", sa.Integer(), nullable=False),
        sa.Column("blk", sa.Integer(), nullable=False),
        sa.Column("tov", sa.Integer(), nullable=False),
        sa.Column("pf", sa.Integer(), nullable=False),
        sa.Column("plus_minus", sa.Integer(), nullable=False),
        sa.Column("ts_pct", sa.Double[float](), nullable=True),
        sa.Column("efg_pct", sa.Double[float](), nullable=True),
        sa.Column("usg_pct", sa.Double[float](), nullable=True),
        sa.Column("ast_pct", sa.Double[float](), nullable=True),
        sa.Column("tov_pct", sa.Double[float](), nullable=True),
        sa.Column("oreb_pct", sa.Double[float](), nullable=True),
        sa.Column("dreb_pct", sa.Double[float](), nullable=True),
        sa.Column("off_rating", sa.Double[float](), nullable=True),
        sa.Column("def_rating", sa.Double[float](), nullable=True),
        sa.Column("pace", sa.Double[float](), nullable=True),
        sa.Column("deflections", sa.Integer(), nullable=True),
        sa.Column("contested_shots", sa.Integer(), nullable=True),
        sa.Column("charges_drawn", sa.Integer(), nullable=True),
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
            "dnp_reason IN ('cd', 'injury', 'inactive', 'suspension', 'g_league', 'other')",
            name=op.f("ck_player_game_dnp_reason"),
        ),
        sa.CheckConstraint("source IN ('stats', 'cdn')", name=op.f("ck_player_game_source")),
        sa.ForeignKeyConstraint(
            ["game_id"], ["game.game_id"], name=op.f("fk_player_game_game_id_game")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_player_game_team_id_team")
        ),
        sa.PrimaryKeyConstraint("game_id", "player_id", name=op.f("pk_player_game")),
    )
    op.create_index(
        op.f("ix_player_game_player_id_team_id"),
        "player_game",
        ["player_id", "team_id"],
        unique=False,
    )
    op.create_table(
        "possession",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("offense_team_id", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Double[float](), nullable=False),
        sa.Column("end_sec", sa.Double[float](), nullable=False),
        sa.Column("start_score_margin", sa.Integer(), nullable=False),
        sa.Column("start_type", sa.String(), nullable=True),
        sa.Column("has_timeout", sa.Boolean(), nullable=False),
        sa.Column("previous_has_timeout", sa.Boolean(), nullable=False),
        sa.Column("end_type", sa.String(), nullable=False),
        sa.Column("counted", sa.Boolean(), nullable=False),
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
            ["game_id"], ["game.game_id"], name=op.f("fk_possession_game_id_game")
        ),
        sa.ForeignKeyConstraint(
            ["offense_team_id"],
            ["team.nba_team_id"],
            name=op.f("fk_possession_offense_team_id_team"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_possession")),
        sa.UniqueConstraint(
            "game_id", "period", "number", name=op.f("uq_possession_game_id_period_number")
        ),
    )
    op.create_table(
        "shot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("event_num", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("clock_sec", sa.Integer(), nullable=False),
        sa.Column("x", sa.Integer(), nullable=False),
        sa.Column("y", sa.Integer(), nullable=False),
        sa.Column("distance_ft", sa.Integer(), nullable=False),
        sa.Column("zone", sa.String(), nullable=False),
        sa.Column("shot_type", sa.String(), nullable=False),
        sa.Column("made", sa.Boolean(), nullable=False),
        sa.Column("assisted_by", sa.Integer(), nullable=True),
        sa.Column("shot_clock_bucket", sa.String(), nullable=True),
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
            "zone IN ('rim', 'short_mid', 'long_mid', 'corner_3', 'above_break_3')",
            name=op.f("ck_shot_zone"),
        ),
        sa.ForeignKeyConstraint(["game_id"], ["game.game_id"], name=op.f("fk_shot_game_id_game")),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_shot_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shot")),
        sa.UniqueConstraint("game_id", "event_num", name=op.f("uq_shot_game_id_event_num")),
    )
    op.create_index(
        op.f("ix_shot_player_id_game_id"), "shot", ["player_id", "game_id"], unique=False
    )
    op.create_table(
        "stint",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Double[float](), nullable=False),
        sa.Column("end_sec", sa.Double[float](), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("lineup_key", sa.String(), nullable=False),
        sa.Column("opp_lineup_key", sa.String(), nullable=False),
        sa.Column("possessions", sa.Integer(), nullable=False),
        sa.Column("pts_for", sa.Integer(), nullable=False),
        sa.Column("pts_against", sa.Integer(), nullable=False),
        sa.Column("is_garbage_time", sa.Boolean(), nullable=True),
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
        sa.ForeignKeyConstraint(["game_id"], ["game.game_id"], name=op.f("fk_stint_game_id_game")),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.nba_team_id"], name=op.f("fk_stint_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stint")),
        sa.UniqueConstraint(
            "game_id",
            "team_id",
            "period",
            "start_sec",
            name=op.f("uq_stint_game_id_team_id_period_start_sec"),
        ),
    )


def downgrade() -> None:
    op.drop_table("stint")
    op.drop_index(op.f("ix_shot_player_id_game_id"), table_name="shot")
    op.drop_table("shot")
    op.drop_table("possession")
    op.drop_index(op.f("ix_player_game_player_id_team_id"), table_name="player_game")
    op.drop_table("player_game")
    op.drop_table("matchup")
    op.drop_table("roster_membership")
    op.drop_index(op.f("ix_game_game_date"), table_name="game")
    op.drop_table("game")
    op.drop_table("team")
    op.drop_table("player")

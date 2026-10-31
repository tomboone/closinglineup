import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

BACKEND = Path(__file__).parents[1]

BASELINE = "62f166b83d8c"
BASELINE_TABLES = {"load_log", "job_run"}
# Spec §6.1, §6.2, §6.3 and §6.4's tables.
DATA_TABLES = {
    "team",
    "player",
    "roster_membership",
    "game",
    "player_game",
    "stint",
    "possession",
    "possession_lineup",
    "shot",
    "matchup",
    "injury_status",
    "transaction",
    "impact_snapshot",
    "contract",
    "rotation_state",
    "rotation_event",
    "player_flag",
}


def alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    """Run the Alembic CLI as `uv run alembic` would, from `backend/`, reading `pyproject.toml`."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def test_migrations_have_one_head() -> None:
    script = ScriptDirectory.from_config(Config(toml_file=str(BACKEND / "pyproject.toml")))

    assert len(script.get_heads()) == 1


def _tables(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_head_creates_every_table_and_leaves_no_drift(database_url: str) -> None:
    upgrade = alembic("upgrade", "head", database_url=database_url)
    assert upgrade.returncode == 0, upgrade.stderr

    assert _tables(database_url) >= BASELINE_TABLES | DATA_TABLES

    check = alembic("check", database_url=database_url)
    assert check.returncode == 0, check.stdout + check.stderr


def test_upgrade_seeds_the_30_teams(database_url: str) -> None:
    upgrade = alembic("upgrade", "head", database_url=database_url)
    assert upgrade.returncode == 0, upgrade.stderr

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM team")).scalar_one()
    finally:
        engine.dispose()
    assert count == 30


def test_downgrade_to_the_baseline_drops_the_data_tables_cleanly(database_url: str) -> None:
    assert alembic("upgrade", "head", database_url=database_url).returncode == 0

    downgrade = alembic("downgrade", BASELINE, database_url=database_url)
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        tables = _tables(database_url)
        assert not tables & DATA_TABLES
        assert tables >= BASELINE_TABLES
    finally:
        # Leave the database at head for every other test, which upgrades and asserts on it
        # itself; asserting here would mask a failed downgrade.
        alembic("upgrade", "head", database_url=database_url)


def _columns(database_url: str, table: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


# NEU-1873: Hoops Rumors' non-guaranteed contracts in place of Spotrac's cap pages.
CONTRACT_FROM_HOOPS_RUMORS = "e347b161377e"
CONTRACT_KEY_AND_TIMESTAMPS = {
    "id",
    "player_id",
    "team_id",
    "season",
    "as_of_date",
    "source",
    "created_at",
    "updated_at",
}


def test_the_contract_migration_upgrades_and_downgrades_its_columns(database_url: str) -> None:
    assert alembic("upgrade", "head", database_url=database_url).returncode == 0
    assert _columns(database_url, "contract") == CONTRACT_KEY_AND_TIMESTAMPS | {
        "salary",
        "minimum_salary",
        "guaranteed",
        "guarantee_date",
        "guaranteed_after",
    }

    downgrade = alembic("downgrade", f"{CONTRACT_FROM_HOOPS_RUMORS}-1", database_url=database_url)
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        assert _columns(database_url, "contract") == CONTRACT_KEY_AND_TIMESTAMPS | {
            "salary",
            "guaranteed",
            "option_type",
            "option_date",
            "trade_eligible_date",
            "two_way",
        }
    finally:
        alembic("upgrade", "head", database_url=database_url)


# NEU-1983: each game's tip-off, from the schedule.
GAME_TIP_TIME = "a3f1c2d4e5b6"


def test_the_tip_time_migration_adds_and_drops_its_column(database_url: str) -> None:
    assert alembic("upgrade", "head", database_url=database_url).returncode == 0
    assert "tip_time" in _columns(database_url, "game")

    downgrade = alembic("downgrade", f"{GAME_TIP_TIME}-1", database_url=database_url)
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        assert "tip_time" not in _columns(database_url, "game")
    finally:
        alembic("upgrade", "head", database_url=database_url)


# NEU-1996: the value metrics beside the impact ratings.
IMPACT_VALUE_SOURCES = "b4d8e2f1c907"
VORP_ROW = (
    "INSERT INTO impact_snapshot (date, player_id, source, value) "
    "VALUES ('2026-10-20', 990001, 'vorp_local', 1.5)"
)


def test_the_value_sources_migration_admits_and_then_drops_vorp_and_ws(database_url: str) -> None:
    assert alembic("upgrade", "head", database_url=database_url).returncode == 0
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(VORP_ROW))

        downgrade = alembic("downgrade", f"{IMPACT_VALUE_SOURCES}-1", database_url=database_url)
        try:
            assert downgrade.returncode == 0, downgrade.stderr
            with engine.connect() as connection:
                left = connection.scalar(
                    text("SELECT count(*) FROM impact_snapshot WHERE player_id = 990001")
                )
                assert left == 0
                with pytest.raises(IntegrityError):
                    connection.execute(text(VORP_ROW))
        finally:
            alembic("upgrade", "head", database_url=database_url)
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM impact_snapshot WHERE player_id = 990001"))
        engine.dispose()

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).parents[1]


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


def test_upgrade_head_creates_the_baseline_tables_and_leaves_no_drift(database_url: str) -> None:
    upgrade = alembic("upgrade", "head", database_url=database_url)
    assert upgrade.returncode == 0, upgrade.stderr

    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"load_log", "job_run"} <= tables

    check = alembic("check", database_url=database_url)
    assert check.returncode == 0, check.stdout + check.stderr

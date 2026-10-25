import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from closinglineup import overlays
from closinglineup.db.models import JobRun
from closinglineup.overlays import Overlay
from tests.test_migrations import alembic


@pytest.fixture
def database_url() -> str:
    """A reachable Postgres: the compose `postgres` service locally, the service container in CI.

    Tests that take this fixture skip on a host with no `DATABASE_URL`, so a bare
    `uv run pytest` outside the container still runs everything else.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run inside `docker compose exec app`")
    return url


@pytest.fixture
def db(database_url: str) -> Iterator[sessionmaker[Session]]:
    """Sessions on the migrated database, with `job_run` emptied first."""
    upgrade = alembic("upgrade", "head", database_url=database_url)
    assert upgrade.returncode == 0, upgrade.stderr
    engine: Engine = create_engine(database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.execute(delete(JobRun))
    try:
        yield sessions
    finally:
        engine.dispose()


@pytest.fixture
def behind(database_url: str) -> Iterator[str]:
    """The database one revision behind head, put back at head afterwards."""
    assert alembic("upgrade", "head", database_url=database_url).returncode == 0
    downgrade = alembic("downgrade", "-1", database_url=database_url)
    assert downgrade.returncode == 0, downgrade.stderr
    try:
        yield database_url
    finally:
        alembic("upgrade", "head", database_url=database_url)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Overlay]:
    """An empty overlay registry in place of the real one, for the test to register fakes in."""
    fresh: dict[str, Overlay] = {}
    monkeypatch.setattr(overlays, "REGISTRY", fresh)
    return fresh

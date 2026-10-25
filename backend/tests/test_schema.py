from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from closinglineup.clock import FakeClock
from closinglineup.db.schema import (
    POLL_SECONDS,
    SchemaNotAtHeadError,
    script_head,
    wait_for_schema_head,
)
from tests.test_migrations import alembic

START = datetime(2026, 10, 25, 12, tzinfo=UTC)
TIMEOUT = timedelta(seconds=12)


def test_at_head_returns_without_sleeping(database_url: str) -> None:
    assert alembic("upgrade", "head", database_url=database_url).returncode == 0
    engine = create_engine(database_url)
    clock = FakeClock(START)
    try:
        wait_for_schema_head(engine, timeout=TIMEOUT, clock=clock)
    finally:
        engine.dispose()

    assert clock.sleeps == []


def test_one_revision_behind_raises_after_the_timeout(behind: str) -> None:
    engine = create_engine(behind)
    clock = FakeClock(START)
    try:
        with pytest.raises(SchemaNotAtHeadError, match=script_head()):
            wait_for_schema_head(engine, timeout=TIMEOUT, clock=clock)
    finally:
        engine.dispose()

    poll = timedelta(seconds=POLL_SECONDS)
    assert clock.sleeps == [START + poll, START + 2 * poll, START + TIMEOUT]


class UpgradingClock(FakeClock):
    """A FakeClock that migrates the database to head during its first sleep, as `app` would."""

    def __init__(self, start: datetime, database_url: str) -> None:
        super().__init__(start)
        self.database_url = database_url

    def sleep_until(self, when: datetime) -> None:
        if not self.sleeps:
            upgrade = alembic("upgrade", "head", database_url=self.database_url)
            assert upgrade.returncode == 0, upgrade.stderr
        super().sleep_until(when)


def test_reaching_head_mid_wait_returns(behind: str) -> None:
    engine = create_engine(behind)
    clock = UpgradingClock(START, behind)
    try:
        wait_for_schema_head(engine, timeout=TIMEOUT, clock=clock)
    finally:
        engine.dispose()

    assert clock.sleeps == [START + timedelta(seconds=POLL_SECONDS)]


def test_an_unreachable_database_is_waited_on_like_a_stale_one() -> None:
    engine = create_engine("postgresql+psycopg://unused@127.0.0.1:1/x")
    clock = FakeClock(START)
    try:
        with pytest.raises(SchemaNotAtHeadError, match="unreachable"):
            wait_for_schema_head(engine, timeout=TIMEOUT, clock=clock)
    finally:
        engine.dispose()

    assert clock.sleeps[-1] == START + TIMEOUT

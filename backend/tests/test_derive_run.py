from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import FakeClock
from closinglineup.db.models import LoadLog, RunStatus
from closinglineup.derive import run
from closinglineup.derive.run import (
    RECONCILE,
    WRITES,
    DeriveError,
    DeriveRules,
    derive,
    missing_dates,
    raise_for_failures,
)
from closinglineup.load.loader import LOAD_STEP
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache

# Dates no other test loads or derives, with no games.
DAYS = [date(1999, 1, d) for d in (4, 5, 6)]
START = datetime(1999, 1, 7, 8, tzinfo=UTC)


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(LoadLog).where(LoadLog.game_date.between(DAYS[0], DAYS[-1])))


@pytest.fixture
def clean(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    _clear(db)
    try:
        yield db
    finally:
        _clear(db)


@pytest.fixture
def rules() -> DeriveRules:
    unused = "postgresql+psycopg://unused@127.0.0.1:1/x"
    return DeriveRules.from_settings(Settings(database_url=unused, tz=ZoneInfo("UTC")))


def _log(db: sessionmaker[Session], day: date, table: str, step: str, status: RunStatus) -> None:
    with db.begin() as session:
        session.merge(
            LoadLog(table_name=table, game_date=day, step=step, status=status, started_at=START)
        )


def _logged(db: sessionmaker[Session]) -> list[tuple[date, str, str]]:
    with db() as session:
        rows = session.execute(
            select(LoadLog.game_date, LoadLog.step, LoadLog.status)
            .where(LoadLog.game_date.between(DAYS[0], DAYS[-1]), LoadLog.step != LOAD_STEP)
            .order_by(LoadLog.game_date, LoadLog.step)
        )
        return [(day, step, status) for day, step, status in rows]


def test_missing_dates_are_the_loaded_ones_whose_writing_steps_have_not_all_succeeded(
    clean: sessionmaker[Session],
) -> None:
    for day in DAYS:
        _log(clean, day, "player_game", LOAD_STEP, RunStatus.SUCCEEDED)
    # The first is derived; the second's flags failed; the third was never derived.
    for step in WRITES:
        _log(clean, DAYS[0], step.table, step.name, RunStatus.SUCCEEDED)
        status = RunStatus.FAILED if step.name == "flags" else RunStatus.SUCCEEDED
        _log(clean, DAYS[1], step.table, step.name, status)

    assert missing_dates(clean, DAYS[0], DAYS[-1]) == DAYS[1:]
    assert missing_dates(clean, DAYS[0], DAYS[1]) == DAYS[1:2]


def test_a_date_with_a_failed_load_is_not_due_a_derive(clean: sessionmaker[Session]) -> None:
    _log(clean, DAYS[0], "player_game", LOAD_STEP, RunStatus.FAILED)
    # Its box scores loaded, but its stints did not.
    _log(clean, DAYS[1], "player_game", LOAD_STEP, RunStatus.SUCCEEDED)
    _log(clean, DAYS[1], "stint", LOAD_STEP, RunStatus.FAILED)

    assert missing_dates(clean, DAYS[0], DAYS[-1]) == []


def test_derive_records_every_step_of_every_date_and_is_then_not_missing(
    clean: sessionmaker[Session], rules: DeriveRules, tmp_path: Path
) -> None:
    for day in DAYS:
        _log(clean, day, "player_game", LOAD_STEP, RunStatus.SUCCEEDED)

    results = derive(clean, RawCache(tmp_path), FakeClock(START), DAYS, rules)

    # Every writing step for each date, in date order, and one reconciliation, for the last date.
    assert [(r.day, r.step) for r in results if r.step != run.WINDOWS] == [
        *((day, run.GARBAGE_TIME) for day in DAYS),
        *((day, step) for day in DAYS for step in WRITES[2:]),
        (DAYS[-1], RECONCILE),
    ]
    assert [r.day for r in results if r.step == run.WINDOWS] == DAYS
    assert all(r.status is RunStatus.SUCCEEDED for r in results)
    # No game on the date, so nothing to reconcile.
    assert results[-1].detail == {
        "skipped": "no final game on the date in a season type with lineups"
    }
    assert {status for *_, status in _logged(clean)} == {"succeeded"}
    assert len(_logged(clean)) == len(DAYS) * len(WRITES) + 1
    assert missing_dates(clean, DAYS[0], DAYS[-1]) == []
    raise_for_failures(results)


def test_a_step_that_fails_is_recorded_and_stops_the_run(
    clean: sessionmaker[Session],
    rules: DeriveRules,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fails_on_the_second(db: Any, day: date, rule: Any) -> dict[str, Any]:
        if day == DAYS[1]:
            raise ValueError("no events today")
        return {"events": 0, "removed": 0}

    monkeypatch.setattr(run, "compute_events", fails_on_the_second)
    for day in DAYS:
        _log(clean, day, "player_game", LOAD_STEP, RunStatus.SUCCEEDED)

    results = derive(clean, RawCache(tmp_path), FakeClock(START), DAYS, rules)

    failed = results[-1]
    assert (failed.day, failed.step, failed.status) == (DAYS[1], run.EVENTS, RunStatus.FAILED)
    assert failed.detail == {"error": "ValueError: no events today"}
    # Nothing after it ran: not the date's flags, nor the next date, nor reconciliation.
    assert (DAYS[1], "flags", "succeeded") not in _logged(clean)
    assert (DAYS[1], "events", "failed") in _logged(clean)
    assert not [r for r in results if r.day == DAYS[2] and r.step in (run.TIERS, RECONCILE)]
    assert missing_dates(clean, DAYS[0], DAYS[-1]) == DAYS[1:]
    with pytest.raises(DeriveError, match="1999-01-05 events: ValueError: no events today"):
        raise_for_failures(results)


def test_derive_of_no_dates_does_nothing(
    clean: sessionmaker[Session], rules: DeriveRules, tmp_path: Path
) -> None:
    assert derive(clean, RawCache(tmp_path), FakeClock(START), [], rules) == []

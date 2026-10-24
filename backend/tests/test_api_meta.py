from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.api.app import create_app
from closinglineup.db.models import ImpactSnapshot, InjuryStatus, JobRun, LoadLog
from closinglineup.derive.run import WRITES
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN

# Dates later than any other test seeds, so the latest of each is this file's.
DERIVED = date(2031, 3, 1)
# Every step but the last has succeeded: not yet derived.
PARTLY = date(2031, 3, 2)
EARLY = datetime.combine(PARTLY, time(10), EASTERN)
LATE = datetime.combine(PARTLY, time(13), EASTERN)
BOS = 1610612738
NYK = 1610612752
PLAYERS = [9_300_101, 9_300_102, 9_300_103]


def _injury(player_id: int, team_id: int, at: datetime, source: str) -> dict[str, Any]:
    return {
        "report_datetime": at,
        "game_date": at.date(),
        "player_id": player_id,
        "team_id": team_id,
        "status": "Out",
        "reason": "Injury/Illness - Left Ankle; Sprain",
        "source": source,
    }


def _log(day: date, table: str, step: str) -> dict[str, Any]:
    now = datetime.combine(day, time(4), EASTERN)
    return {
        "table_name": table,
        "game_date": day,
        "step": step,
        "status": "succeeded",
        "started_at": now,
        "finished_at": now,
    }


def _run(job: str, hour: int, status: str) -> dict[str, Any]:
    at = datetime.combine(PARTLY, time(hour), EASTERN)
    return {"job": job, "started_at": at, "finished_at": at, "status": status}


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(InjuryStatus).where(InjuryStatus.player_id.in_(PLAYERS)))
        session.execute(delete(ImpactSnapshot).where(ImpactSnapshot.player_id.in_(PLAYERS)))
        session.execute(delete(LoadLog).where(LoadLog.game_date.in_([DERIVED, PARTLY])))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """NYK's newest injury row ESPN's, BOS's tied between the two sources; a derived date and a
    later one part-derived; two runs of `nightly`, one of `injuries`, and an EPM snapshot."""
    _clear(db)
    with db.begin() as session:
        session.execute(
            insert(InjuryStatus),
            [
                _injury(PLAYERS[0], NYK, EARLY, "pdf"),
                _injury(PLAYERS[0], NYK, LATE, "espn"),
                _injury(PLAYERS[1], BOS, LATE, "pdf"),
                _injury(PLAYERS[2], BOS, LATE, "espn"),
            ],
        )
        session.execute(
            insert(LoadLog),
            [_log(DERIVED, *step) for step in WRITES]
            + [_log(PARTLY, *step) for step in WRITES[:-1]],
        )
        session.execute(
            insert(JobRun),
            [
                _run("nightly", 3, "failed"),
                _run("nightly", 5, "succeeded"),
                _run("injuries", 10, "running"),
            ],
        )
        session.execute(
            insert(ImpactSnapshot),
            {"date": PARTLY, "player_id": PLAYERS[0], "source": "epm", "value": 2.0},
        )
    yield db
    _clear(db)


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        yield client


def test_meta_reports_espn_as_the_injury_source_where_its_row_is_newest(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    response = client.get("/api/meta")

    assert response.status_code == 200
    markers = {m["tricode"]: m for m in response.json()["injury_sources"]}
    assert markers["NYK"] == {"tricode": "NYK", "source": "espn", "stale": True}
    # A tie goes to the official report.
    assert markers["BOS"] == {"tricode": "BOS", "source": "pdf", "stale": False}


def test_the_latest_derived_date_is_the_latest_with_every_writing_step_succeeded(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    assert client.get("/api/meta").json()["latest_derived_date"] == DERIVED.isoformat()


def test_meta_lists_each_jobs_latest_run(seeded: sessionmaker[Session], client: TestClient) -> None:
    jobs = {run["job"]: run for run in client.get("/api/meta").json()["jobs"]}

    assert set(jobs) == {"nightly", "injuries"}
    assert jobs["nightly"]["status"] == "succeeded"
    assert jobs["injuries"]["status"] == "running"


def test_freshness_is_each_sources_newest_row(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    freshness = {f["source"]: f["latest"] for f in client.get("/api/meta").json()["freshness"]}

    assert set(freshness) == {
        "box_score",
        "injury_pdf",
        "injury_espn",
        "transactions",
        "epm",
        "darko",
        "bpm_local",
        "contracts",
    }
    assert datetime.fromisoformat(freshness["injury_pdf"]) == LATE
    assert datetime.fromisoformat(freshness["injury_espn"]) == LATE
    assert freshness["epm"] == PARTLY.isoformat()

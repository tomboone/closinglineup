from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from closinglineup.api.app import create_app
from closinglineup.cli import app as cli
from closinglineup.db.models import Game, LoadLog
from closinglineup.derive.run import WRITES
from closinglineup.scheduler import DRIFT_STEP
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN

# A season no other test seeds, so its final game and its derived date are the latest of each.
SEASON = "2040-41"
FINAL = date(2040, 11, 10)
GAME_ID = "0024000001"
BOS = 1610612738
NYK = 1610612752

type Seed = Callable[[int], None]


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(Game).where(Game.game_id == GAME_ID))
        session.execute(
            delete(LoadLog).where(LoadLog.game_date.between(FINAL - timedelta(days=7), FINAL))
        )


@pytest.fixture
def seed(db: sessionmaker[Session]) -> Iterator[Seed]:
    """Seeds a final game on `FINAL` and every derive step succeeded `behind` days before it."""

    def seed(behind: int) -> None:
        derived = FINAL - timedelta(days=behind)
        now = datetime.now(EASTERN)
        with db.begin() as session:
            session.execute(
                insert(Game),
                [
                    {
                        "game_id": GAME_ID,
                        "season": SEASON,
                        "game_date": FINAL,
                        "phase": "regular",
                        "home_team_id": BOS,
                        "away_team_id": NYK,
                        "status": "final",
                        "home_score": 100,
                        "away_score": 90,
                        "home_b2b": False,
                        "away_b2b": False,
                    }
                ],
            )
            session.execute(
                insert(LoadLog),
                [
                    {
                        "table_name": step.table,
                        "game_date": derived,
                        "step": step.name,
                        "status": "succeeded",
                        "started_at": now,
                        "finished_at": now,
                    }
                    for step in WRITES
                ],
            )

    _clear(db)
    yield seed
    _clear(db)


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        yield client


@pytest.mark.parametrize("behind", [0, 1])
def test_health_is_200_when_the_latest_derived_date_is_within_36_h_of_the_last_final_game(
    seed: Seed, client: TestClient, behind: int
) -> None:
    seed(behind)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "reason": None}


def test_health_is_503_when_the_latest_derived_date_is_3_days_behind(
    seed: Seed, client: TestClient
) -> None:
    seed(3)

    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == (
        "the latest derived date, 2040-11-07, is more than 36 h behind the latest final game's, "
        "2040-11-10"
    )


@pytest.mark.parametrize(("behind", "exit_code"), [(0, 0), (3, 1)])
def test_the_health_command_applies_the_same_rule(
    seed: Seed,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    behind: int,
    exit_code: int,
) -> None:
    seed(behind)
    monkeypatch.setenv("DATABASE_URL", database_url)

    result = CliRunner().invoke(cli, ["health"])

    assert result.exit_code == exit_code, result.output
    expected = "health: ok" if exit_code == 0 else "behind the latest final game's"
    assert expected in result.output


def _seed_drift(db: sessionmaker[Session], age: timedelta) -> None:
    """A `drift` row of `FINAL`'s fetch, finished `age` ago; `_clear` removes it."""
    finished = datetime.now(EASTERN) - age
    with db.begin() as session:
        session.execute(
            insert(LoadLog),
            [
                {
                    "table_name": "nba_stats/boxscoretraditionalv2",
                    "game_date": FINAL,
                    "step": DRIFT_STEP,
                    "status": "drift",
                    "started_at": finished,
                    "finished_at": finished,
                    "detail": {"drift": {GAME_ID: "missing PlayerStats column MIN"}},
                }
            ],
        )


@pytest.mark.parametrize(("hours", "exit_code"), [(1, 1), (25, 0)])
def test_the_health_command_exits_1_while_a_drift_row_is_under_24_h_old(
    seed: Seed,
    db: sessionmaker[Session],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    hours: int,
    exit_code: int,
) -> None:
    seed(0)
    _seed_drift(db, timedelta(hours=hours))
    monkeypatch.setenv("DATABASE_URL", database_url)

    result = CliRunner().invoke(cli, ["health"])

    assert result.exit_code == exit_code, result.output
    expected = (
        "health: ok"
        if exit_code == 0
        else "health: schema drift in the last 24 h: nba_stats/boxscoretraditionalv2"
    )
    assert expected in result.output

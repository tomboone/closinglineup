import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from closinglineup import cli, injuries
from closinglineup.cli import app
from closinglineup.clock import Clock, FakeClock
from closinglineup.db.models import Game, InjuryStatus, JobRun, Player
from closinglineup.heartbeat import Heartbeat
from closinglineup.names import NameIndex, load_aliases
from closinglineup.overlays import by_name
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN, parse, report_url
from closinglineup.sources.nba_http import NbaSession, RateLimiter
from closinglineup.teams import by_tricode
from tests.fakes import Connect, RoutedGet
from tests.test_injury_pdf import FIXTURES, WEEKEND
from tests.test_scheduler import make_jobs

runner = CliRunner()

PLAYERS: list[tuple[int, str]] = [
    (player_id, name) for player_id, name in json.loads((FIXTURES / "players.json").read_text())
]
DAY = date(2026, 3, 1)
REPORTED = datetime(2026, 3, 1, 17, 15, tzinfo=EASTERN)
# POR at ATL on the weekend fixture's date, under a game ID no real game has.
GAME_ID = "0022599901"


def _index() -> NameIndex:
    return NameIndex(PLAYERS, load_aliases())


def test_every_line_but_an_unknown_player_resolves_to_a_player_and_team(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = parse(WEEKEND)
    games = {(DAY, by_tricode("POR").nba_team_id, by_tricode("ATL").nba_team_id): GAME_ID}

    resolved = injuries.resolve_rows(report, _index(), games, {})

    # Tamar Bates is not in NBA.com's player list; "Yang, Hansen" resolves through the alias.
    assert resolved.unresolved == ["Bates, Tamar (DEN 2026-03-01)"]
    assert len(resolved.rows) == len(report.rows) - 1
    assert all(row["player_id"] and row["team_id"] for row in resolved.rows)
    assert any(row["player_id"] == 1642905 for row in resolved.rows)
    assert "injury report player not resolved" in caplog.text
    por = [row for row in resolved.rows if row["team_id"] == by_tricode("POR").nba_team_id]
    assert por and all(row["game_id"] == GAME_ID for row in por)
    assert "POR@ATL 2026-03-01" not in resolved.unmatched_games
    assert "MIN@DEN 2026-03-01" in resolved.unmatched_games


def test_the_overlay_is_registered_in_the_injuries_family() -> None:
    overlay = by_name("injuries")

    assert (overlay.family, overlay.run) == ("injuries", injuries.run)


def _stored(db: sessionmaker[Session]) -> list[tuple[Any, ...]]:
    with db() as session:
        rows = session.scalars(
            select(InjuryStatus)
            .where(InjuryStatus.report_datetime == REPORTED)
            .order_by(InjuryStatus.player_id, InjuryStatus.game_date)
        )
        return [
            (r.game_date, r.game_id, r.player_id, r.team_id, r.status, r.reason, r.source)
            for r in rows
        ]


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(InjuryStatus).where(InjuryStatus.report_datetime == REPORTED))
        session.execute(delete(Game).where(Game.game_id == GAME_ID))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """The fixture reports' players, and POR at ATL on the weekend fixture's date."""
    _clear(db)
    with db.begin() as session:
        players = [{"nba_player_id": pid, "name": name} for pid, name in PLAYERS]
        session.execute(insert(Player).values(players).on_conflict_do_nothing())
        session.add(
            Game(
                game_id=GAME_ID,
                season="2025-26",
                game_date=DAY,
                phase="regular",
                home_team_id=by_tricode("ATL").nba_team_id,
                away_team_id=by_tricode("POR").nba_team_id,
                status="final",
                home_b2b=False,
                away_b2b=False,
            )
        )
    yield db
    _clear(db)


def test_the_overlay_stores_the_report_and_a_rerun_leaves_it_as_it_was(
    seeded: sessionmaker[Session], tmp_path: Path
) -> None:
    get = RoutedGet({report_url(REPORTED): WEEKEND.read_bytes()})
    # The jobs get their database from `seeded`, never from the settings.
    settings = Settings(database_url="postgresql+psycopg://unused@127.0.0.1:1/x", tz=EASTERN)
    clock = FakeClock(datetime(2026, 3, 1, 17, 20, tzinfo=EASTERN))
    with httpx.Client() as http:
        jobs = make_jobs(settings, clock, tmp_path, seeded, get, Heartbeat({}, http))
        detail = jobs.overlay("injuries", DAY)
        first = _stored(seeded)
        again = jobs.overlay("injuries", DAY)

    assert len(first) == detail["rows"] == 133
    assert {row[-1] for row in first} == {"pdf"}
    assert detail["statuses"] == {
        "Out": 105,
        "Questionable": 10,
        "Doubtful": 2,
        "Probable": 1,
        "Available": 15,
    }
    assert detail["unresolved"] == ["Bates, Tamar (DEN 2026-03-01)"]
    assert len(detail["not_submitted"]) == 6
    assert _stored(seeded) == first
    assert again == detail
    # The second run read the PDF the first cached.
    assert len(get.calls) == 1


def test_overlays_injuries_date_stores_the_report_with_source_pdf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
    seeded: sessionmaker[Session],
) -> None:
    # A past date's latest report is its 11:45 PM one; serve the fixture there.
    last = datetime(2026, 3, 1, 23, 45, tzinfo=EASTERN)
    get = RoutedGet({report_url(last): WEEKEND.read_bytes()})

    def fake_session(settings: Settings, clock: Clock) -> NbaSession:
        return NbaSession(Connect(get), RateLimiter(1e6, clock), clock)

    monkeypatch.setattr(cli, "make_nba_session", fake_session)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    result = runner.invoke(app, ["overlays", "injuries", "--date", "2026-03-01"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "overlays injuries 2026-03-01: succeeded"
    stored = _stored(seeded)
    assert len(stored) == 133
    assert {row[-1] for row in stored} == {"pdf"}
    with seeded() as session:
        run = session.scalars(select(JobRun).where(JobRun.job == "overlay:injuries")).one()
    assert run.status == "succeeded"

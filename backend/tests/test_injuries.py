import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
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
from closinglineup.sources import espn_injuries
from closinglineup.sources.injury_pdf import (
    EASTERN,
    InjuryReport,
    InjuryStatusRow,
    parse,
    report_url,
)
from closinglineup.sources.nba_http import NbaSession, RateLimiter
from closinglineup.teams import by_espn_id, by_tricode
from tests.fakes import Connect, RoutedGet
from tests.test_espn_injuries import FIXTURES as ESPN_FIXTURES
from tests.test_espn_injuries import REPORT as ESPN_REPORT
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
ESPN_PLAYERS: list[tuple[int, str]] = [
    (player_id, name)
    for player_id, name in json.loads((ESPN_FIXTURES / "players.json").read_text())
]
ESPN_DAY = date(2026, 10, 23)
ESPN_REPORTED = datetime(2026, 10, 23, 14, 45, 50, tzinfo=UTC)
# The jobs get their database from the test's fixture, never from the settings.
SETTINGS = Settings(database_url="postgresql+psycopg://unused@127.0.0.1:1/x", tz=EASTERN)


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
    assert resolved.duplicates == []


def test_a_player_named_twice_for_a_date_keeps_his_first_line_and_lists_the_second() -> None:
    line = InjuryStatusRow(DAY, "POR", "ATL", "POR", "Yang, Hansen", "Out", None)
    report = InjuryReport(REPORTED, [line, replace(line, status="Available")], [])

    resolved = injuries.resolve_rows(report, _index(), {}, {})

    assert [row["status"] for row in resolved.rows] == ["Out"]
    assert resolved.duplicates == ["Yang, Hansen (POR 2026-03-01)"]


def test_the_overlay_is_registered_in_the_injuries_family() -> None:
    overlay = by_name("injuries")

    assert (overlay.family, overlay.run) == ("injuries", injuries.run)


def _stored(db: sessionmaker[Session], at: datetime = REPORTED) -> list[tuple[Any, ...]]:
    with db() as session:
        rows = session.scalars(
            select(InjuryStatus)
            .where(InjuryStatus.report_datetime == at)
            .order_by(InjuryStatus.player_id, InjuryStatus.game_date)
        )
        return [
            (r.game_date, r.game_id, r.player_id, r.team_id, r.status, r.reason, r.source)
            for r in rows
        ]


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(
            delete(InjuryStatus).where(InjuryStatus.report_datetime.in_([REPORTED, ESPN_REPORTED]))
        )
        session.execute(delete(Game).where(Game.game_id == GAME_ID))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """The fixture reports' players, ESPN's included, and POR at ATL on the weekend fixture's
    date."""
    _clear(db)
    with db.begin() as session:
        players = [{"nba_player_id": pid, "name": name} for pid, name in PLAYERS + ESPN_PLAYERS]
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


@respx.mock(assert_all_called=False)
def test_the_overlay_stores_the_report_and_a_rerun_leaves_it_as_it_was(
    seeded: sessionmaker[Session], tmp_path: Path
) -> None:
    get = RoutedGet({report_url(REPORTED): WEEKEND.read_bytes()})
    espn = respx.get(espn_injuries.URL).respond(200, content=ESPN_REPORT.read_bytes())
    clock = FakeClock(datetime(2026, 3, 1, 17, 20, tzinfo=EASTERN))
    with httpx.Client() as http:
        jobs = make_jobs(SETTINGS, clock, tmp_path, seeded, get, Heartbeat({}, http))
        detail = jobs.overlay("injuries", DAY)
        first = _stored(seeded)
        again = jobs.overlay("injuries", DAY)

    assert not espn.called
    assert detail["source"] == "pdf"
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


def _espn_teams() -> set[int]:
    """The NBA.com ID of every team with an injury in the ESPN fixture, read from it by hand: each
    athlete's own team."""
    data = json.loads(ESPN_REPORT.read_text())
    return {
        by_espn_id(int(injury["athlete"]["team"]["id"])).nba_team_id
        for group in data["injuries"]
        for injury in group["injuries"]
    }


def _no_lines(path: Path) -> InjuryReport:
    return InjuryReport(REPORTED, [], [])


def _run_injuries(
    db: sessionmaker[Session], tmp_path: Path, day: date, routes: dict[str, bytes]
) -> dict[str, Any]:
    """The overlay's detail for `day`, run at 10:00 that morning with the PDFs in `routes`."""
    clock = FakeClock(datetime.combine(day, datetime.min.time(), EASTERN).replace(hour=10))
    with httpx.Client() as http:
        jobs = make_jobs(SETTINGS, clock, tmp_path, db, RoutedGet(routes), Heartbeat({}, http))
        return jobs.overlay("injuries", day)


@pytest.mark.parametrize(
    "pdf", [None, b"%PDF-1.7 not an injury report"], ids=["not published", "not readable"]
)
@respx.mock
def test_a_report_not_had_falls_back_to_espn_for_every_team_with_an_injury(
    seeded: sessionmaker[Session], tmp_path: Path, pdf: bytes | None
) -> None:
    ten = datetime(2026, 10, 23, 10, 0, tzinfo=EASTERN)
    routes = {} if pdf is None else {report_url(ten): pdf}
    espn = respx.get(espn_injuries.URL).respond(200, content=ESPN_REPORT.read_bytes())

    detail = _run_injuries(seeded, tmp_path, ESPN_DAY, routes)

    stored = _stored(seeded, ESPN_REPORTED)
    assert espn.called
    assert detail["source"] == "espn"
    assert detail["pdf_error"]
    assert len(stored) == detail["rows"] == 79
    assert detail["statuses"] == {"Out": 50, "Questionable": 29}
    assert (detail["unresolved"], detail["unmapped"], detail["duplicates"]) == ([], [], [])
    assert {row[-1] for row in stored} == {"espn"}
    assert {(row[0], row[1]) for row in stored} == {(ESPN_DAY, None)}
    assert {row[3] for row in stored} == _espn_teams()


@respx.mock
def test_a_report_with_no_lines_on_a_day_with_games_falls_back_to_espn(
    seeded: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(injuries, "parse", _no_lines)
    espn = respx.get(espn_injuries.URL).respond(200, content=ESPN_REPORT.read_bytes())
    ten = datetime(2026, 3, 1, 10, 0, tzinfo=EASTERN)

    detail = _run_injuries(seeded, tmp_path, DAY, {report_url(ten): WEEKEND.read_bytes()})

    assert espn.called
    assert detail["source"] == "espn"
    assert detail["pdf_error"] == "2026-03-01T1000.pdf has no lines on a day with games"
    assert len(_stored(seeded, ESPN_REPORTED)) == 79


@respx.mock(assert_all_called=False)
def test_a_report_with_no_lines_on_a_day_without_games_stands(
    seeded: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(injuries, "parse", _no_lines)
    espn = respx.get(espn_injuries.URL).respond(200, content=ESPN_REPORT.read_bytes())
    day = date(2026, 2, 17)
    ten = datetime(2026, 2, 17, 10, 0, tzinfo=EASTERN)

    detail = _run_injuries(seeded, tmp_path, day, {report_url(ten): WEEKEND.read_bytes()})

    assert not espn.called
    assert (detail["source"], detail["rows"]) == ("pdf", 0)

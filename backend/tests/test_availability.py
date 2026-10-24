from collections.abc import Iterator
from datetime import date, datetime

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import InjuryStatus
from closinglineup.derive.availability import latest_injury_source
from closinglineup.sources.injury_pdf import EASTERN
from closinglineup.teams import by_tricode

DAY = date(2020, 1, 15)
TEAM = by_tricode("WAS").nba_team_id
OTHER = by_tricode("BOS").nba_team_id
PLAYER = 1


def _at(day: int, hour: int) -> datetime:
    return datetime(2020, 1, day, hour, tzinfo=EASTERN)


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(
            delete(InjuryStatus).where(
                InjuryStatus.report_datetime >= _at(1, 0), InjuryStatus.report_datetime < _at(31, 0)
            )
        )


@pytest.fixture
def rows(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """Injury rows in January 2020, which no real report has, removed before and after."""
    _clear(db)
    yield db
    _clear(db)


def _store(db: sessionmaker[Session], *rows: tuple[int, datetime, str]) -> None:
    with db.begin() as session:
        session.execute(
            insert(InjuryStatus),
            [
                {
                    "report_datetime": at,
                    "game_date": at.date(),
                    "player_id": PLAYER,
                    "team_id": team,
                    "status": "Out",
                    "source": source,
                }
                for team, at, source in rows
            ],
        )


def _source(db: sessionmaker[Session], team: int = TEAM, day: date = DAY) -> str | None:
    with db() as session:
        return latest_injury_source(session, team, day)


def test_espn_when_it_is_newer_than_every_official_row(rows: sessionmaker[Session]) -> None:
    _store(rows, (TEAM, _at(15, 10), "pdf"), (TEAM, _at(15, 13), "espn"))

    assert _source(rows) == "espn"


def test_pdf_when_an_official_row_is_newer_than_espns(rows: sessionmaker[Session]) -> None:
    _store(rows, (TEAM, _at(15, 13), "espn"), (TEAM, _at(15, 16), "pdf"))

    assert _source(rows) == "pdf"


def test_pdf_when_an_official_row_is_as_new_as_espns(rows: sessionmaker[Session]) -> None:
    _store(rows, (TEAM, _at(15, 13), "espn"), (TEAM, _at(15, 13), "pdf"))

    assert _source(rows) == "pdf"


def test_only_rows_reported_by_the_end_of_the_day_count(rows: sessionmaker[Session]) -> None:
    _store(rows, (TEAM, _at(15, 23), "espn"), (TEAM, _at(16, 0), "pdf"))

    assert _source(rows) == "espn"
    assert _source(rows, day=date(2020, 1, 16)) == "pdf"


def test_only_the_teams_own_rows_count(rows: sessionmaker[Session]) -> None:
    _store(rows, (TEAM, _at(15, 10), "pdf"), (OTHER, _at(15, 13), "espn"))

    assert _source(rows) == "pdf"
    assert _source(rows, team=OTHER) == "espn"


def test_none_for_a_team_with_no_rows(rows: sessionmaker[Session]) -> None:
    assert _source(rows) is None

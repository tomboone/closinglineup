import json
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup import transactions
from closinglineup.clock import FakeClock
from closinglineup.db.models import Transaction
from closinglineup.heartbeat import Heartbeat
from closinglineup.overlays import by_name
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, RateLimiter
from closinglineup.sources.transactions import (
    ENDPOINT,
    SOURCE,
    URL,
    TransactionRow,
    classify,
    fetch,
    parse,
)
from closinglineup.teams import by_tricode
from tests.fakes import Connect, RoutedGet
from tests.test_scheduler import make_jobs

FIXTURES = Path(__file__).parent / "fixtures" / SOURCE / ENDPOINT
DAY = date(2026, 10, 23)
FEED = FIXTURES / f"{DAY.isoformat()}.json"
HIELD = 1627741
# The jobs get their database from the test's fixture, never from the settings.
SETTINGS = Settings(database_url="postgresql+psycopg://unused@127.0.0.1:1/x")


def _feed() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FEED.read_text())
    return payload


def test_every_row_of_the_fixture_classifies_into_a_type_and_none_is_other() -> None:
    movement = parse(_feed())

    # 636 rows: 24 trades of draft consideration name no player, and one two-way signing of
    # Ernest Udeh, Jr. on 2026-07-01 is listed twice. Counts taken from each row's
    # `Transaction_Type` and description, not from the parser.
    assert (len(movement.rows), movement.no_player, movement.duplicates) == (611, 24, 1)
    assert Counter(row.type for row in movement.rows) == {
        "waive": 309,
        "sign": 165,
        "two_way_sign": 69,
        "trade": 57,
        "two_way_convert": 11,
    }
    other = [row.detail for row in movement.rows if row.type == "other"]
    # Each `other` row is listed here for review; under 5% of the rows may be.
    assert other == []
    assert len(other) < 0.05 * len(movement.rows)
    assert movement.unknown_teams == []


def test_a_trade_is_from_the_team_its_description_names_to_the_team_it_is_listed_for() -> None:
    hield = [row for row in parse(_feed()).rows if row.player_id == HIELD]

    assert hield == [
        TransactionRow(
            date(2026, 9, 27),
            "trade",
            HIELD,
            by_tricode("CHA").nba_team_id,
            by_tricode("CHI").nba_team_id,
            "Chicago Bulls received guard Buddy Hield from Charlotte Hornets.",
        ),
        TransactionRow(
            date(2026, 9, 22),
            "trade",
            HIELD,
            by_tricode("ATL").nba_team_id,
            by_tricode("CHA").nba_team_id,
            "Charlotte Hornets received guard Buddy Hield from Atlanta Hawks.",
        ),
    ]


def test_a_waiver_is_from_its_team_and_a_signing_to_it() -> None:
    hawkins = [row for row in parse(_feed()).rows if row.player_id == 1641722]
    memphis, chicago = by_tricode("MEM").nba_team_id, by_tricode("CHI").nba_team_id

    assert [(r.date, r.type, r.from_team_id, r.to_team_id) for r in hawkins] == [
        (date(2026, 10, 5), "two_way_sign", None, chicago),
        (date(2026, 10, 2), "waive", memphis, None),
        (date(2026, 9, 8), "trade", by_tricode("NOP").nba_team_id, memphis),
    ]


@pytest.mark.parametrize(
    ("transaction_type", "description", "expected"),
    [
        ("Signing", "Dallas Mavericks signed guard X to a Two-Way Contract.", "two_way_sign"),
        ("Signing", "Utah Jazz signed guard X to a 10-Day Contract.", "sign"),
        ("Signing", "Phoenix Suns re-signed forward X to a Veteran Extension.", "sign"),
        ("AwardOnWaivers", "Phoenix Suns claimed forward X off waivers.", "sign"),
        ("Waive", "Utah Jazz waived forward X.", "waive"),
        ("Trade", "Chicago Bulls received guard X from Charlotte Hornets.", "trade"),
        (
            "ContractConverted",
            "… converted the contract of X to an NBA Contract.",
            "two_way_convert",
        ),
        # Not in the feed: classified from the description.
        ("Assignment", "Boston Celtics assigned guard X to the Maine Celtics.", "assign"),
        ("Recall", "Boston Celtics recalled guard X from the Maine Celtics.", "recall"),
        ("Option", "Miami Heat exercised the team option on forward X.", "option_exercise"),
        ("Option", "X declined his player option with the Miami Heat.", "option_decline"),
        ("Suspension", "The NBA suspended guard X for one game.", "other"),
    ],
)
def test_classify(transaction_type: str, description: str, expected: str) -> None:
    assert classify(transaction_type, description) == expected


def _session(get: RoutedGet, clock: FakeClock) -> NbaSession:
    return NbaSession(Connect(get), RateLimiter(1e6, clock), clock)


def test_fetch_caches_the_feed_under_the_date_and_a_second_fetch_reads_it(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 23, 13, tzinfo=UTC))
    get = RoutedGet({URL: FEED.read_bytes()})
    cache = RawCache(tmp_path, clock)

    with _session(get, clock) as session:
        first = fetch(session, cache, DAY)
        second = fetch(session, cache, DAY)

    assert first == second == _feed()
    assert cache.list(SOURCE, ENDPOINT) == ["2026-10-23"]
    assert len(get.calls) == 1


def test_fetch_refuses_a_feed_with_no_rows(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 23, 13, tzinfo=UTC))
    empty = json.dumps({"NBA_Player_Movement": {"rows": [], "columns": []}}).encode()
    cache = RawCache(tmp_path, clock)

    with _session(RoutedGet({URL: empty}), clock) as session, pytest.raises(NbaHttpError):
        fetch(session, cache, DAY)

    assert cache.list(SOURCE, ENDPOINT) == []


def test_the_overlay_is_registered_in_the_daily_family() -> None:
    overlay = by_name("transactions")

    assert (overlay.family, overlay.run) == ("daily", transactions.run)


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(Transaction))


@pytest.fixture
def empty(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    _clear(db)
    yield db
    _clear(db)


def _stored(db: sessionmaker[Session]) -> list[tuple[Any, ...]]:
    with db() as session:
        found = session.scalars(select(Transaction).order_by(Transaction.id))
        return [
            (
                r.id,
                r.date,
                r.type,
                r.player_id,
                r.from_team_id,
                r.to_team_id,
                r.detail,
                r.updated_at,
            )
            for r in found
        ]


def test_the_overlay_stores_the_feed_and_a_rerun_on_the_same_day_changes_nothing(
    empty: sessionmaker[Session], tmp_path: Path
) -> None:
    get = RoutedGet({URL: FEED.read_bytes()})
    clock = FakeClock(datetime(2026, 10, 23, 13, tzinfo=UTC))
    with httpx.Client() as http:
        jobs = make_jobs(SETTINGS, clock, tmp_path, empty, get, Heartbeat({}, http))
        first = jobs.overlay("transactions", DAY)
        stored = _stored(empty)
        again = jobs.overlay("transactions", DAY)

    assert (first["rows"], first["changed"], first["removed"]) == (611, 611, 0)
    assert first["types"]["waive"] == 309
    assert (first["other"], first["no_player"], first["duplicates"]) == ([], 24, 1)
    assert len(stored) == 611
    assert (again["rows"], again["changed"], again["removed"]) == (611, 0, 0)
    # Not a row rewritten: the same IDs and `updated_at`s.
    assert _stored(empty) == stored
    # The second run read the feed the first cached.
    assert len(get.calls) == 1


def test_the_overlay_removes_a_row_the_feed_no_longer_holds_and_updates_a_changed_one(
    empty: sessionmaker[Session], tmp_path: Path
) -> None:
    feed = _feed()
    rows: list[dict[str, Any]] = feed["NBA_Player_Movement"]["rows"]
    get = RoutedGet({URL: json.dumps(feed).encode()})
    clock = FakeClock(datetime(2026, 10, 22, 13, tzinfo=UTC))
    with httpx.Client() as http:
        jobs = make_jobs(SETTINGS, clock, tmp_path, empty, get, Heartbeat({}, http))
        jobs.overlay("transactions", date(2026, 10, 22))
        # The next day's feed drops Hield's first trade and names another team for the second.
        hield = [i for i, row in enumerate(rows) if "Buddy Hield" in row["TRANSACTION_DESCRIPTION"]]
        rows[hield[0]]["TEAM_ID"] = float(by_tricode("BOS").nba_team_id)
        del rows[hield[1]]
        get.routes[URL] = json.dumps(feed).encode()
        detail = jobs.overlay("transactions", DAY)

    assert (detail["rows"], detail["changed"], detail["removed"]) == (610, 1, 1)
    with empty() as session:
        to_teams = session.scalars(
            select(Transaction.to_team_id).where(Transaction.player_id == HIELD)
        ).all()
        assert to_teams == [by_tricode("BOS").nba_team_id]
        assert session.scalar(select(func.count()).select_from(Transaction)) == 610


def test_a_feed_cut_short_at_its_old_end_keeps_the_history_it_no_longer_holds(
    empty: sessionmaker[Session], tmp_path: Path
) -> None:
    feed = _feed()
    get = RoutedGet({URL: json.dumps(feed).encode()})
    clock = FakeClock(datetime(2026, 10, 22, 13, tzinfo=UTC))
    with httpx.Client() as http:
        jobs = make_jobs(SETTINGS, clock, tmp_path, empty, get, Heartbeat({}, http))
        jobs.overlay("transactions", date(2026, 10, 22))
        root = feed["NBA_Player_Movement"]
        root["rows"] = [r for r in root["rows"] if r["TRANSACTION_DATE"] >= "2026-10-01"]
        get.routes[URL] = json.dumps(feed).encode()
        detail = jobs.overlay("transactions", DAY)

    assert (detail["since"], detail["changed"], detail["removed"]) == ("2026-10-01", 0, 0)
    with empty() as session:
        assert session.scalar(select(func.count()).select_from(Transaction)) == 611

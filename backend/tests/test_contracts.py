from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup import contracts
from closinglineup.clock import FakeClock
from closinglineup.contracts import Built, ContractRow, Openers, build_rows, replace_contracts
from closinglineup.db.models import Contract, Player
from closinglineup.heartbeat import Heartbeat
from closinglineup.names import NameIndex
from closinglineup.overlays import by_name
from closinglineup.resolve import name_index
from closinglineup.scheduler import Jobs
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.hoopsrumors import (
    ARTICLES,
    CRAWL_DELAY,
    GuaranteeLine,
    HoopsRumorsError,
    NonGuaranteedLine,
    fetch,
    parse_guarantee_dates,
    parse_non_guaranteed,
)
from closinglineup.teams import TEAMS, by_tricode
from tests.fakes import RoutedGet
from tests.test_scheduler import make_jobs

FIXTURES = Path(__file__).parent / "fixtures" / "hoopsrumors"
LISTED = FIXTURES / "non_guaranteed" / "2026-10-23.html"
DATES = FIXTURES / "guarantee_dates" / "2026-10-23.html"
DAY = date(2026, 10, 23)
SEASON = "2026-27"
DEFAULT = date(2027, 1, 7)
# 04:30 Eastern on the day, as the `weekly` job runs.
FETCHED = datetime(2026, 10, 23, 8, 30, tzinfo=UTC)
URLS = ARTICLES[SEASON]
OPENERS = Openers(date(2026, 10, 20), {})
DET = by_tricode("DET").nba_team_id
CLE = by_tricode("CLE").nba_team_id
NOP = by_tricode("NOP").nba_team_id
UTA = by_tricode("UTA").nba_team_id
# The jobs get their database from the test's fixture, never from the settings.
UNUSED_DB = "postgresql+psycopg://unused@127.0.0.1:1/x"
ENABLED = Settings(database_url=UNUSED_DB, hoopsrumors_enabled=True)
DISABLED = Settings(database_url=UNUSED_DB, hoopsrumors_enabled=False)
# Player IDs for the fixture's names in a test's `player` table, clear of NBA.com's.
FIRST_TEST_ID = 9_100_000


def _page(body: str, title: str = "2026/27 Non-Guaranteed Contracts By Team") -> str:
    return (
        f'<html><body><h1 class="entry-title">{title}</h1>'
        f'<div class="entry-content">{body}</div><ul><li>Not the article</li></ul></body></html>'
    )


def _teams(players: dict[str, str] | None = None, *, count: int = 30) -> str:
    """Every team's heading, `None` under it unless `players` gives its list's lines."""
    players = players or {}
    return "".join(
        f"<h3>{team.name}</h3><ul>{players.get(team.tricode, '<li>None</li>')}</ul>"
        for team in TEAMS[:count]
    )


def _dates(body: str) -> str:
    return _page(f"<p>Intro.</p><hr />{body}", "Early NBA Salary Guarantee Dates For 2026/27")


def _write(tmp_path: Path, html: str) -> Path:
    path = tmp_path / "page.html"
    path.write_text(html, encoding="utf-8")
    return path


def _jobs(
    settings: Settings, tmp_path: Path, db: sessionmaker[Session], http: httpx.Client
) -> Jobs:
    return make_jobs(settings, FakeClock(FETCHED), tmp_path, db, RoutedGet({}), Heartbeat({}, http))


# The non-guaranteed list.


def test_the_fixture_list_parses_every_player_under_his_team() -> None:
    listed = parse_non_guaranteed(LISTED)

    # Counted from the page, not the parser (fixtures/hoopsrumors/README.md).
    assert len(listed.lines) == 22
    assert len({line.team_id for line in listed.lines}) == 15
    assert listed.updated == date(2026, 10, 22)
    by_player = {line.player: line for line in listed.lines}
    assert by_player["Duncan Robinson"] == NonGuaranteedLine(
        DET, "Duncan Robinson", 15_992_957, 2_000_000
    )
    assert by_player["Tolu Smith"] == NonGuaranteedLine(DET, "Tolu Smith", None, None)
    assert by_player["Trendon Watford"] == NonGuaranteedLine(NOP, "Trendon Watford", None, 500_000)
    # A curly apostrophe is kept as the page has it; names are matched without punctuation.
    assert "Jae\u2019Sean Tate" in by_player
    # The Clippers are "Los Angeles Clippers" on the page, and have no one listed.
    assert by_tricode("LAC").nba_team_id not in {line.team_id for line in listed.lines}


def test_a_list_reads_salaries_partials_and_none(tmp_path: Path) -> None:
    body = _teams(
        {
            "BOS": "<li><strong><a>A One</a></strong> ($3,000,000)</li>"
            "<li><strong>B Two</strong><ul><li><em>Partially guaranteed for $250,000.</em></li>"
            "</ul></li>",
        }
    )
    page = _page(f"<p>Intro</p><hr /><p><em>Updated 1-5-27 (9:00 am</em><em> CT)</em></p>{body}")

    listed = parse_non_guaranteed(_write(tmp_path, page))

    bos = by_tricode("BOS").nba_team_id
    assert listed.updated == date(2027, 1, 5)
    assert listed.lines == [
        NonGuaranteedLine(bos, "A One", 3_000_000, None),
        NonGuaranteedLine(bos, "B Two", None, 250_000),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_teams(count=29), "29 team headings, fewer than 30"),
        ("<h3>Seattle SuperSonics</h3>" + _teams(), "'Seattle SuperSonics' names no NBA team"),
        (
            _teams({"BOS": "<li>A One<ul><li>Exhibit 10.</li></ul></li>"}),
            "unreadable note 'Exhibit 10.'",
        ),
        (
            _teams({"BOS": "<li>Exhibit 10 deals: 2</li>"}),
            "unreadable player line 'Exhibit 10 deals: 2'",
        ),
    ],
)
def test_a_list_that_is_not_the_layout_raises(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(HoopsRumorsError, match=message):
        parse_non_guaranteed(_write(tmp_path, _page(body)))


# The guarantee dates.


def test_the_fixture_dates_parse_every_line_and_leave_named_headings_unresolved() -> None:
    lines = parse_guarantee_dates(DATES, SEASON)

    # Counted from the page (fixtures/hoopsrumors/README.md).
    assert len(lines) == 38
    assert sum(line.settled for line in lines) == 36
    unsettled = [line for line in lines if not line.settled]
    assert unsettled == [
        GuaranteeLine(date(2026, 11, 15), CLE, "Mario Hezonja", 0, 2_845_883, 2_845_883, False),
        GuaranteeLine(
            date(2026, 12, 30), NOP, "Trendon Watford", 500_000, 2_845_883, 2_845_883, False
        ),
    ]
    assert [line.day for line in lines].count("league_opener") == 6
    assert [line.day for line in lines].count("team_opener") == 4
    assert lines[0] == GuaranteeLine(
        date(2026, 6, 28),
        by_tricode("ATL").nba_team_id,
        "Buddy Hield",
        3_000_000,
        9_658_536,
        9_658_536,
        True,
    )
    increases = [line for line in lines if line.salary is None]
    assert [(line.player, line.before, line.after) for line in increases] == [
        ("Mo Bamba", 250_000, 500_000),
        ("Dwight Powell", 1_000_000, 1_500_000),
        ("Trendon Watford", 250_000, 500_000),
    ]
    # "Sixers" and "Trail Blazers" are nicknames `teams.py` spells otherwise or in two words.
    teams = {line.player: line.team_id for line in lines}
    assert teams["Adem Bona"] == by_tricode("PHI").nba_team_id
    assert teams["Sidy Cissoko"] == by_tricode("POR").nba_team_id


def test_a_date_heading_in_january_to_may_is_in_the_seasons_second_year(tmp_path: Path) -> None:
    page = _dates(
        "<p><strong>January 5</strong></p><ul><li><strong>A One</strong> (<strong>Jazz</strong>):"
        " Non-guaranteed salary ($2,000,000) becomes partially guaranteed ($1,000,000).</li></ul>"
    )

    (line,) = parse_guarantee_dates(_write(tmp_path, page), SEASON)

    assert line == GuaranteeLine(date(2027, 1, 5), UTA, "A One", 0, 2_000_000, 1_000_000, False)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("<p><strong>Someday</strong></p>", "'Someday' is not a date heading"),
        (
            "<ul><li>A One (Jazz): Non-guaranteed salary ($1) becomes fully guaranteed.</li></ul>",
            "a line before any date heading",
        ),
        ("<p>June 28</p><ul><li>A One (Jazz): Waived.</li></ul>", "unreadable guarantee line"),
        (
            "<p>June 28</p><ul><li>A One (Jazz): Partial guarantee ($1) becomes fully guaranteed."
            "</li></ul>",
            "gives no full salary",
        ),
        (
            "<p>June 28</p><ul><li>A One (Seattle): Non-guaranteed salary ($1) becomes fully "
            "guaranteed.</li></ul>",
            "'Seattle' names no one NBA team",
        ),
    ],
)
def test_dates_that_are_not_the_layout_raise(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(HoopsRumorsError, match=message):
        parse_guarantee_dates(_write(tmp_path, _dates(body)), SEASON)


# Rows.


def _fixture_index() -> tuple[NameIndex, dict[str, int]]:
    """An index of the list's players, under test IDs."""
    ids = {
        line.player: FIRST_TEST_ID + i for i, line in enumerate(parse_non_guaranteed(LISTED).lines)
    }
    return NameIndex([(player_id, name) for name, player_id in ids.items()], {}), ids


def _built() -> tuple[Built, dict[str, int]]:
    index, ids = _fixture_index()
    built = build_rows(
        parse_non_guaranteed(LISTED).lines,
        parse_guarantee_dates(DATES, SEASON),
        DAY,
        SEASON,
        index,
        {},
        OPENERS,
    )
    return built, ids


def _row(player_id: int, team_id: int) -> ContractRow:
    """A minimum-salary contract with no partial guarantee, for `DAY`."""
    return ContractRow(player_id, team_id, SEASON, None, True, 0, DEFAULT, None, DAY, "hoopsrumors")


def test_the_fixture_builds_a_row_per_player_with_his_next_guarantee_date() -> None:
    built, ids = _built()

    by_id = {row.player_id: row for row in built.rows}
    assert len(built.rows) == 22
    assert by_id[ids["Trendon Watford"]] == ContractRow(
        ids["Trendon Watford"],
        NOP,
        SEASON,
        2_845_883,
        True,
        500_000,
        date(2026, 12, 30),
        2_845_883,
        DAY,
        "hoopsrumors",
    )
    assert by_id[ids["Mario Hezonja"]] == ContractRow(
        ids["Mario Hezonja"],
        CLE,
        SEASON,
        2_845_883,
        True,
        0,
        date(2026, 11, 15),
        2_845_883,
        DAY,
        "hoopsrumors",
    )
    assert by_id[ids["Duncan Robinson"]] == ContractRow(
        ids["Duncan Robinson"],
        DET,
        SEASON,
        15_992_957,
        False,
        2_000_000,
        DEFAULT,
        15_992_957,
        DAY,
        "hoopsrumors",
    )
    assert by_id[ids["Tolu Smith"]] == ContractRow(
        ids["Tolu Smith"], DET, SEASON, None, True, 0, DEFAULT, None, DAY, "hoopsrumors"
    )
    others = set(by_id) - {ids[p] for p in ("Trendon Watford", "Mario Hezonja")}
    assert {by_id[player_id].guarantee_date for player_id in others} == {DEFAULT}
    # Mo Bamba's salary is his guarantee-date line's, though the list gives none.
    assert by_id[ids["Mo Bamba"]].salary == 3_506_659
    assert (built.unresolved, built.aliases_to_add, built.duplicates) == ([], [], [])
    # 38 lines, of which Bamba's two, Watford's two, Hezonja's, Crawford's and Brandon Williams'
    # are for a listed player and team.
    assert len(built.ignored_lines) == 31
    assert "Buddy Hield (ATL)" in built.ignored_lines


def test_an_unresolved_or_repeated_player_has_no_row_and_is_listed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    index = NameIndex([(1, "A One"), (2, "Twin"), (3, "Twin")], {})
    listed = [
        NonGuaranteedLine(DET, "Nobody Known", None, None),
        NonGuaranteedLine(DET, "Twin", None, None),
        NonGuaranteedLine(DET, "A One", None, None),
        NonGuaranteedLine(UTA, "A One", 2_000_000, None),
        NonGuaranteedLine(UTA, "Twin", None, None),
    ]

    built = build_rows(listed, [], DAY, SEASON, index, {(UTA, DAY): {3}}, OPENERS)

    assert [(row.player_id, row.team_id) for row in built.rows] == [(1, DET), (3, UTA)]
    assert built.unresolved == ["Nobody Known (DET)", "Twin (DET)"]
    assert built.aliases_to_add == ["Nobody Known: <player ID>", "Twin: <player ID>"]
    assert built.duplicates == ["A One (UTA)"]
    assert "Hoops Rumors player not resolved" in caplog.text
    assert "Hoops Rumors lists a player twice" in caplog.text


def test_a_named_heading_resolves_to_the_opener_and_a_past_or_other_teams_line_is_passed_over(
    caplog: pytest.LogCaptureFixture,
) -> None:
    index = NameIndex([(1, "A One"), (2, "B Two"), (3, "C Three")], {})
    listed = [
        NonGuaranteedLine(UTA, "A One", None, None),
        NonGuaranteedLine(UTA, "B Two", None, None),
        NonGuaranteedLine(UTA, "C Three", 3_000_000, None),
    ]
    dates = [
        GuaranteeLine(date(2026, 10, 1), UTA, "A One", 0, 2_000_000, 2_000_000, False),
        GuaranteeLine("team_opener", UTA, "A One", 0, 2_000_000, 1_000_000, False),
        GuaranteeLine(date(2026, 11, 1), DET, "A One", 0, 2_000_000, 2_000_000, False),
        GuaranteeLine("league_opener", UTA, "B Two", 0, 2_100_000, 2_100_000, False),
        GuaranteeLine(date(2026, 12, 1), UTA, "B Two", 0, 2_100_000, 2_100_000, False),
        GuaranteeLine("team_opener", UTA, "C Three", 0, None, 500_000, False),
    ]
    openers = Openers(date(2026, 10, 20), {UTA: date(2026, 10, 24)})

    built = build_rows(listed, dates, DAY, SEASON, index, {}, openers)

    assert [(r.salary, r.guarantee_date, r.guaranteed_after) for r in built.rows] == [
        # Utah's opener, the day after the run date; the October 1 line is past and the
        # Pistons' is another contract's.
        (2_000_000, date(2026, 10, 24), 1_000_000),
        # The league's opener is past, so December 1 is next.
        (2_100_000, date(2026, 12, 1), 2_100_000),
        (3_000_000, date(2026, 10, 24), 500_000),
    ]
    assert built.ignored_lines == ["A One (DET)"]

    unknown = build_rows(listed[:1], dates[1:2], DAY, SEASON, index, {}, Openers(None, {}))

    assert unknown.rows[0].guarantee_date == DEFAULT
    assert "Hoops Rumors guarantee date not known" in caplog.text


# Fetching.


def _routes(listed: bytes | None = None, dates: bytes | None = None) -> tuple[respx.Route, ...]:
    return (
        respx.get(URLS.non_guaranteed).respond(200, content=listed or LISTED.read_bytes()),
        respx.get(URLS.guarantee_dates).respond(200, content=dates or DATES.read_bytes()),
    )


@respx.mock
def test_fetch_caches_both_pages_a_crawl_delay_apart_and_a_rerun_reads_them_back(
    tmp_path: Path,
) -> None:
    routes = _routes()
    clock = FakeClock(FETCHED)
    cache = RawCache(tmp_path, clock)

    with httpx.Client() as client:
        paths = fetch(client, cache, DAY, SEASON)
        again = fetch(client, cache, DAY, SEASON)

    raw = tmp_path / "raw" / "hoopsrumors"
    assert (
        paths
        == again
        == (
            raw / "non_guaranteed" / "2026-10-23.html",
            raw / "guarantee_dates" / "2026-10-23.html",
        )
    )
    assert paths[0].read_bytes() == LISTED.read_bytes()
    assert paths[1].read_bytes() == DATES.read_bytes()
    assert [route.call_count for route in routes] == [1, 1]
    assert clock.sleeps == [FETCHED + CRAWL_DELAY]
    assert cache.meta("hoopsrumors", "guarantee_dates", "2026-10-23", suffix=".html") == {
        "fetched_at": (FETCHED + CRAWL_DELAY).isoformat(),
        "url": URLS.guarantee_dates,
        "status": 200,
        "params": {},
    }


def test_a_season_with_no_articles_raises_naming_it(tmp_path: Path) -> None:
    with (
        httpx.Client() as client,
        pytest.raises(HoopsRumorsError, match="no Hoops Rumors articles for 2027-28; add them"),
    ):
        fetch(client, RawCache(tmp_path), date(2027, 10, 25), "2027-28")


@pytest.mark.parametrize(
    ("listed", "dates", "error"),
    [
        (_page(_teams(), title="Page Not Found").encode(), None, "is not the article"),
        (_page(_teams(count=12)).encode(), None, "12 team headings"),
        (None, b"<html>Just a moment...</html>", "is not the article"),
    ],
)
@respx.mock
def test_a_page_that_is_not_its_article_raises_and_neither_is_cached(
    tmp_path: Path, listed: bytes | None, dates: bytes | None, error: str
) -> None:
    _routes(listed, dates)

    with httpx.Client() as client, pytest.raises(HoopsRumorsError, match=error):
        fetch(client, RawCache(tmp_path, FakeClock(FETCHED)), DAY, SEASON)

    assert not (tmp_path / "raw").exists()


@respx.mock
def test_an_error_status_raises_and_is_not_cached(tmp_path: Path) -> None:
    respx.get(URLS.non_guaranteed).respond(503)

    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        fetch(client, RawCache(tmp_path, FakeClock(FETCHED)), DAY, SEASON)

    assert not (tmp_path / "raw").exists()


# The overlay.


def test_the_overlay_is_registered_in_the_weekly_family() -> None:
    overlay = by_name("contracts")

    assert (overlay.family, overlay.run) == ("weekly", contracts.run)


@respx.mock(assert_all_called=False)
def test_the_overlay_is_a_no_op_with_no_request_when_hoopsrumors_is_disabled(
    tmp_path: Path,
) -> None:
    routes = _routes()
    with httpx.Client() as http:
        detail = contracts.run(_jobs(DISABLED, tmp_path, sessionmaker(), http), DAY)

    assert detail == {"enabled": False, "rows": 0}
    assert not any(route.called for route in routes)
    assert not (tmp_path / "raw").exists()


@respx.mock(assert_all_called=False)
def test_a_first_run_for_a_date_other_than_today_raises_before_any_request(
    tmp_path: Path,
) -> None:
    routes = _routes()
    with (
        httpx.Client() as http,
        pytest.raises(
            ValueError, match="the pages are today's, 2026-10-23; 2026-10-16 is not cached"
        ),
    ):
        contracts.run(_jobs(ENABLED, tmp_path, sessionmaker(), http), date(2026, 10, 16))

    assert not any(route.called for route in routes)


LATER = DAY + timedelta(days=7)


def _stored(db: sessionmaker[Session], day: date = DAY) -> list[ContractRow]:
    with db() as session:
        rows = session.scalars(
            select(Contract)
            .where(Contract.as_of_date == day, Contract.source == "hoopsrumors")
            .order_by(Contract.player_id, Contract.team_id)
        )
        return [
            ContractRow(
                r.player_id,
                r.team_id,
                r.season,
                r.salary,
                r.minimum_salary,
                r.guaranteed,
                r.guarantee_date,
                r.guaranteed_after,
                r.as_of_date,
                "hoopsrumors",
            )
            for r in rows
        ]


@pytest.fixture
def cleared(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """The database with no `hoopsrumors` rows for the test's dates, before and after, and each
    fixture list name the `player` table does not already resolve added under a test ID for the
    test's duration."""

    def clear() -> None:
        with db.begin() as session:
            session.execute(
                delete(Contract).where(
                    Contract.as_of_date.in_([DAY, LATER]), Contract.source == "hoopsrumors"
                )
            )

    clear()
    with db.begin() as session:
        index = name_index(session)
        missing = [
            line.player
            for line in parse_non_guaranteed(LISTED).lines
            if index.resolve(line.player) is None
        ]
        added = [FIRST_TEST_ID + i for i in range(len(missing))]
        session.add_all(
            Player(nba_player_id=player_id, name=name)
            for player_id, name in zip(added, missing, strict=True)
        )
    yield db
    clear()
    with db.begin() as session:
        session.execute(delete(Player).where(Player.nba_player_id.in_(added)))


@respx.mock
def test_the_overlay_stores_the_list_and_a_rerun_leaves_it_as_it_was(
    cleared: sessionmaker[Session], tmp_path: Path
) -> None:
    routes = _routes()
    with httpx.Client() as http:
        jobs = _jobs(ENABLED, tmp_path, cleared, http)
        detail = jobs.overlay("contracts", DAY)
        first = _stored(cleared)
        again = jobs.overlay("contracts", DAY)

    ignored = detail.pop("ignored_lines")
    assert detail == {
        "enabled": True,
        "files": ["non_guaranteed/2026-10-23.html", "guarantee_dates/2026-10-23.html"],
        "updated": "2026-10-22",
        "season": SEASON,
        "rows": 22,
        "changed": 22,
        "removed": 0,
        "unresolved": [],
        "aliases_to_add": [],
        "duplicates": [],
    }
    assert len(ignored) == 31
    assert len(first) == 22
    assert {row.guarantee_date for row in first} == {
        date(2026, 11, 15),
        date(2026, 12, 30),
        DEFAULT,
    }
    assert again == {**detail, "ignored_lines": ignored, "changed": 0, "removed": 0}
    assert _stored(cleared) == first
    # The second run read the pages the first cached.
    assert [route.call_count for route in routes] == [1, 1]


def test_replace_contracts_writes_only_what_changed_and_a_later_list_keeps_earlier_rows(
    cleared: sessionmaker[Session],
) -> None:
    kept = _row(1, DET)
    dropped = _row(2, DET)
    with cleared.begin() as session:
        assert replace_contracts(session, DAY, [kept, dropped]) == (2, 0)
    with cleared.begin() as session:
        assert replace_contracts(session, DAY, [kept, dropped]) == (0, 0)

    later = replace(kept, as_of_date=LATER, guaranteed=250_000)
    with cleared.begin() as session:
        assert replace_contracts(session, LATER, [later]) == (1, 0)

    assert _stored(cleared) == [kept, dropped]
    assert _stored(cleared, LATER) == [later]

    with cleared.begin() as session:
        assert replace_contracts(session, DAY, [kept]) == (0, 1)
    with cleared.begin() as session:
        assert replace_contracts(session, DAY, []) == (0, 1)
    assert _stored(cleared) == []
    assert _stored(cleared, LATER) == [later]
